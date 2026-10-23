"""Local, credential-free feed health with temporary network failure backoff."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

logger = logging.getLogger("mtselector.collector.health")
MAX_HEALTH_BYTES = 4 * 1024 * 1024


def state_namespace(network: str) -> str:
    """Keep simple user labels readable without allowing paths outside .state."""
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if (
        isinstance(network, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", network)
        and network.upper() not in reserved
    ):
        return network
    return "network-" + hashlib.sha256(str(network).encode("utf-8")).hexdigest()[:24]


def source_identity(source) -> str:
    """A URL or protocol change starts a new health record; URLs stay in memory."""
    identity = [source.type, source.url, getattr(source, "protocol", None)]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()


def _safe_name(source) -> str:
    label = re.sub(r"(?:https?|file)://\S+", "[feed]", source.name, flags=re.IGNORECASE)
    try:
        parts = urlsplit(source.url)
    except ValueError:
        return "[invalid feed]"
    private = [parts.username, parts.password, *(value for _, value in parse_qsl(parts.query))]
    for value in private:
        if value:
            label = label.replace(unquote(value), "[redacted]")
    return label[:256]


def _finite(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


class SourceHealth:
    """One network and feed family. Failed writes never prevent a proxy scan."""

    def __init__(self, config, kind: str, *, clock=None):
        if kind not in {"mtproto", "web", "webproxy"}:
            raise ValueError("Unknown source health family")
        scan = getattr(config, "scan", None)
        self.enabled = bool(getattr(scan, "source_health_enabled", False))
        self.threshold = getattr(scan, "source_failure_threshold", 3)
        self.cooldown_seconds = getattr(scan, "source_cooldown_minutes", 30) * 60
        self.kind = kind
        folder = getattr(getattr(config, "output", None), "folder", "output")
        network = state_namespace(getattr(scan, "network", "default"))
        self.path = Path(folder) / ".state" / network / kind / "source-health.json"
        self._clock = clock or time.time
        self._entries: dict[str, dict] = {}
        if self.enabled:
            self._load()

    def _warn(self, action: str, exc: Exception):
        # Exception text and paths may contain private feed or network labels.
        logger.warning("Source health %s failed (%s): %s", action, self.kind, type(exc).__name__)

    def _load(self):
        try:
            with self.path.open("rb") as handle:
                body = handle.read(MAX_HEALTH_BYTES + 1)
            if len(body) > MAX_HEALTH_BYTES:
                raise ValueError("Source health exceeds size limit")
            data = json.loads(body)
            if (
                not isinstance(data, dict)
                or data.get("version") != 1
                or not isinstance(data.get("entries"), dict)
            ):
                raise ValueError("Invalid source health state")
            for key, value in data["entries"].items():
                if (
                    not isinstance(key, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", key)
                    or not isinstance(value, dict)
                ):
                    continue
                # Rebuild a bounded schema, never carry arbitrary old file fields.
                row = {}
                for name in ("count", "consecutive_failures", "checked", "qualified", "strict_qualified"):
                    number = value.get(name, 0)
                    row[name] = number if type(number) is int and 0 <= number <= 10**12 else 0
                for name in ("last_checked", "next_retry", "quality_checked_at"):
                    stamp = value.get(name)
                    row[name] = stamp if _finite(stamp) else None
                status = value.get("status")
                row["status"] = status if status in {"ok", "empty", "error"} else "empty"
                row["last_fetch_status"] = row["status"]
                row["quality_attribution"] = "first_source_after_deduplication"
                self._entries[key] = row
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            self._warn("read", exc)

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"version": 1, "entries": self._entries}, ensure_ascii=False, allow_nan=False)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=".source-health-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _persist(self):
        if self.enabled:
            try:
                self._save()
            except (OSError, ValueError, TypeError) as exc:
                self._warn("write", exc)

    def cooldown_report(self, source) -> dict | None:
        if not self.enabled or source.url.startswith("file://"):
            return None
        retry = self._entries.get(source_identity(source), {}).get("next_retry")
        if _finite(retry) and retry > self._clock():
            return {
                "name": source.name,
                "status": "error",
                "count": 0,
                "error": "Cooling down after consecutive network failures; retry scheduled automatically",
                "cooldown_until": retry,
            }
        return None

    def record_collection(self, sources, reports, network_errors):
        """Call only after every enabled feed has completed without cancellation."""
        if not self.enabled:
            return
        checked_at = self._clock()
        for source, report, network_error in zip(sources, reports, network_errors):
            key = source_identity(source)
            if "cooldown_until" in report:
                if key in self._entries:
                    self._entries[key]["name"] = _safe_name(source)
                continue
            row = dict(self._entries.get(key, {}))
            failure = bool(network_error) and not source.url.startswith("file://")
            failures = row.get("consecutive_failures", 0) + 1 if failure else 0
            row.update(
                name=_safe_name(source),
                status=report["status"],
                last_fetch_status=report["status"],
                count=report["count"],
                consecutive_failures=failures,
                last_checked=checked_at,
                next_retry=checked_at + self.cooldown_seconds if failures >= self.threshold else None,
            )
            self._entries[key] = row
        self._persist()

    def record_quality(self, sources, counts):
        if not self.enabled:
            return
        checked_at = self._clock()
        for source in sources:
            key = source_identity(source)
            row = self._entries.setdefault(key, {})
            row.update(
                name=_safe_name(source),
                **counts.get(key, {"checked": 0, "qualified": 0, "strict_qualified": 0}),
            )
            row.update(quality_checked_at=checked_at, quality_attribution="first_source_after_deduplication")
        self._persist()

    def rows(self, sources) -> list[dict]:
        if not self.enabled:
            return []
        rows = []
        seen = set()
        for source in sources:
            identity = source_identity(source)
            row = {**self._entries.get(identity, {}), "name": _safe_name(source)}
            if identity in seen:
                row.update(checked=0, qualified=0, strict_qualified=0)
            seen.add(identity)
            rows.append(row)
        return rows


def quality_counts(results, source_by_proxy: dict[str, str], sources) -> dict[str, dict]:
    """Attribute a deduplicated candidate once, to its first configured source."""
    counts = {
        source_identity(source): {"checked": 0, "qualified": 0, "strict_qualified": 0} for source in sources
    }
    seen = set()
    for result in results:
        key = result.proxy.key()
        if key in seen or not result.attempts:
            continue
        seen.add(key)
        identity = source_by_proxy.get(key)
        if identity not in counts:
            continue
        count = counts[identity]
        count["checked"] += 1
        strict = bool(result.strict_eligible)
        qualified = strict if getattr(result.proxy, "secret", None) is not None else result.successes > 0
        count["strict_qualified"] += int(strict)
        count["qualified"] += int(qualified)
    return counts
