"""Bounded, atomic scan checkpoints and network-specific observation history.

Only finished samples and public proxy settings are serialized. Saved scores and
eligibility flags are never accepted as evidence: callers must rescore restored
results and freshly retest candidates before recommending them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime
from pathlib import Path

from tester.secrets import decode_secret, normalize_port, normalize_server
from utils.models import FailureReason, Proxy, ProxyResult, TelegramQualityPolicy, TestSample
from webproxy.models import WebProxy

SCHEMA = 1
MAX_BYTES = 64 * 1024 * 1024
MAX_CANDIDATES = 100_000
MAX_SAMPLES = 10_000
MAX_HISTORY_OBSERVATIONS = 100_000
CLOCK_SKEW_SECONDS = 60
_MODES = {"mtproto", "telegram", "web"}
_PRIVATE_FIELDS = {
    "auth_key",
    "authkey",
    "auth_key_id",
    "session_key",
    "private_key",
    "api_hash",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
}


def _text(value, name, maximum=2048, *, empty=False):
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or (not empty and not value)
        or any(ord(char) < 32 for char in value)
    ):
        raise ValueError(f"Invalid {name}")
    return value


def _number(value, name, *, minimum=0):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value >= minimum
    except OverflowError:
        valid = False
    if not valid:
        raise ValueError(f"Invalid {name}: expected a finite number >= {minimum}")
    return value


def _timestamp(value, name, *, now=None, max_age=None):
    value = _number(value, name, minimum=1)
    now = time.time() if now is None else now
    if value > now + CLOCK_SKEW_SECONDS:
        raise ValueError(f"Invalid {name}: timestamp is in the future")
    if max_age is not None and now - value > max_age:
        raise ValueError(f"Saved scan is stale ({name}); start a fresh scan")
    return value


def _mode(value):
    if not isinstance(value, str) or value not in _MODES:
        raise ValueError("Invalid scan mode")
    return value


def _network(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        raise ValueError("network must be a short label using letters, numbers, '_' or '-'")
    return value


def serialize_proxy(proxy: Proxy | WebProxy) -> dict:
    if isinstance(proxy, Proxy):
        row = {"kind": "mtproto", **proxy.to_dict()}
    elif isinstance(proxy, WebProxy):
        row = {"kind": "web", **proxy.to_dict()}
        row.pop("uri", None)
    else:
        raise ValueError("Unsupported proxy candidate")
    # Constructors are permissive for MTProto: validate even locally-created data.
    deserialize_proxy(row)
    return row


def deserialize_proxy(row: dict) -> Proxy | WebProxy:
    if not isinstance(row, dict):
        raise ValueError("Invalid saved proxy candidate")
    server = normalize_server(row.get("server"))
    port = normalize_port(row.get("port"))
    source = _text(row.get("source", "unknown"), "proxy source", empty=True)
    if row.get("kind") == "mtproto":
        secret = row.get("secret")
        decode_secret(secret)
        tag = row.get("tag")
        if tag is not None:
            _text(tag, "proxy tag", 256, empty=True)
        return Proxy(server, port, secret, source, tag)
    if row.get("kind") == "web":
        return WebProxy(server, port, row.get("protocol"), source)
    raise ValueError("Invalid saved proxy kind")


def _sample(row: dict) -> TestSample:
    if not isinstance(row, dict) or type(row.get("success")) is not bool:
        raise ValueError("Invalid saved sample success flag")
    values = {}
    for name in ("tcp_latency_ms", "handshake_latency_ms", "total_latency_ms"):
        value = row.get(name)
        values[name] = None if value is None else _number(value, f"sample {name}")
    try:
        reason = FailureReason(row.get("failure_reason", "none"))
    except (TypeError, ValueError) as exc:
        raise ValueError("Invalid saved sample failure_reason") from exc
    if row["success"] and reason != FailureReason.NONE:
        raise ValueError("Successful sample cannot have a failure reason")
    level = row.get("verification_level", "transport")
    if not isinstance(level, str) or level not in {"transport", "telegram_session", "web"}:
        raise ValueError("Invalid saved sample verification_level")
    dc = row.get("dc_id")
    if dc is not None and (type(dc) is not int or not 1 <= dc <= 5):
        raise ValueError("Invalid saved sample dc_id")
    pings = row.get("ping_rtts_ms", [])
    if not isinstance(pings, list) or len(pings) > 100:
        raise ValueError("Invalid saved sample ping_rtts_ms")
    pings = [_number(rtt, "sample ping RTT") for rtt in pings]
    return TestSample(
        row["success"],
        **values,
        failure_reason=reason,
        timestamp=_timestamp(row.get("timestamp"), "sample timestamp"),
        verification_level=level,
        dc_id=dc,
        ping_rtts_ms=pings,
    )


def serialize_result(result: ProxyResult) -> dict:
    if not isinstance(result, ProxyResult):
        raise ValueError("Expected a ProxyResult")
    row = {
        "proxy": serialize_proxy(result.proxy),
        "samples": [asdict(sample) for sample in result.samples],
        "verification_profile": result.verification_profile,
        "quality_policy": result.quality_policy.to_dict(),
    }
    deserialize_result(row)
    return row


def deserialize_result(row: dict) -> ProxyResult:
    """Restore validated samples, ignoring any supplied derived ranking fields."""
    if not isinstance(row, dict):
        raise ValueError("Invalid saved result")
    proxy = deserialize_proxy(row.get("proxy"))
    samples = row.get("samples")
    if not isinstance(samples, list) or len(samples) > MAX_SAMPLES:
        raise ValueError("Invalid saved result samples")
    profile = row.get("verification_profile", "transport")
    if not isinstance(profile, str) or profile not in {"transport", "telegram_session", "web"}:
        raise ValueError("Invalid saved result verification_profile")
    if isinstance(proxy, WebProxy) and profile == "telegram_session":
        raise ValueError("Web proxy cannot contain Telegram session evidence")
    if isinstance(proxy, Proxy) and profile == "web":
        raise ValueError("MTProto proxy cannot use a web verification profile")
    policy = row.get("quality_policy", {})
    if not isinstance(policy, dict):
        raise ValueError("Invalid saved quality policy")
    try:
        policy = TelegramQualityPolicy(**policy)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid saved quality policy: {exc}") from exc
    return ProxyResult(
        proxy,
        [_sample(sample) for sample in samples],
        verification_profile=profile,
        quality_policy=policy,
    )


def _json_tree(value, *, now, max_age=None, depth=0):
    """Reject non-JSON values, excessive nesting and accidental key material."""
    if depth > 32:
        raise ValueError("Saved state nesting is too deep")
    if value is None or type(value) is bool:
        return
    if type(value) in (int, float):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError("Saved state contains a non-finite number")
        return
    if isinstance(value, str):
        if len(value) > 256 * 1024:
            raise ValueError("Saved state string is too large")
        return
    if isinstance(value, list):
        if len(value) > MAX_CANDIDATES:
            raise ValueError("Saved state contains too many entries")
        for item in value:
            _json_tree(item, now=now, max_age=max_age, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 2048:
                raise ValueError("Saved state has an invalid field name")
            normalized = key.lower().replace("-", "_")
            if normalized in _PRIVATE_FIELDS:
                raise ValueError(f"Refusing to save private field: {key}")
            if (
                key in {"timestamp", "created_at", "updated_at", "last_tested", "started_at", "completed_at"}
                and item is not None
            ):
                stamp = item
                if isinstance(stamp, str):
                    try:
                        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            raise ValueError("timezone is required")
                        stamp = parsed.timestamp()
                    except (ValueError, OverflowError) as exc:
                        raise ValueError(f"Invalid {key} timestamp") from exc
                _timestamp(stamp, key, now=now, max_age=max_age)
            _json_tree(item, now=now, max_age=max_age, depth=depth + 1)
        return
    raise ValueError("Saved state contains a non-JSON value")


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Invalid state JSON: duplicate field {key}")
        result[key] = value
    return result


def _read(path):
    try:
        with path.open("rb") as handle:
            content = handle.read(MAX_BYTES + 1)
    except FileNotFoundError as exc:
        raise ValueError(f"Saved scan is missing: {path.name}") from exc
    if len(content) > MAX_BYTES:
        raise ValueError("Saved scan exceeds the 64 MiB safety limit")
    try:
        result = json.loads(content, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError(f"Invalid or corrupt state JSON: {exc}") from exc
    if not isinstance(result, dict):
        raise ValueError("Invalid saved state: expected an object")
    return result


def _write(path, payload):
    content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(content) > MAX_BYTES:
        raise ValueError("Saved scan exceeds the 64 MiB safety limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise OSError(f"Could not save scan state {path.name}: {exc}") from exc
    finally:
        if temporary is not None:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def write_json_atomic(path: Path, value) -> None:
    """Write bounded JSON atomically; write failures propagate to the caller."""
    _json_tree(value, now=time.time())
    _write(Path(path), value)


def _mapping(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    raise ValueError("Fingerprint settings must be a dataclass or dictionary")


def scan_fingerprint(config, mode: str) -> str:
    """Hash scan semantics; source URLs only exist inside their own digest."""
    mode = _mode(mode)
    testing = _mapping(config.testing)
    for name in list(testing):
        if name.startswith(("checkpoint", "resume", "history", "state_")):
            testing.pop(name)
    output = _mapping(config.output)
    output.pop("folder", None)
    sources = config.web_sources if mode == "web" else config.sources
    identities = []
    for source in sources:
        source = _mapping(source)
        source["url"] = hashlib.sha256(str(source.get("url", "")).encode("utf-8")).hexdigest()
        identities.append(source)
    semantics = {
        "schema": SCHEMA,
        "mode": mode,
        "testing": testing,
        "scoring": _mapping(config.scoring),
        "output": output,
        "sources": identities,
    }
    if getattr(config, "scan", None) is not None:
        scan = _mapping(config.scan)
        for name in list(scan):
            if name == "network" or name.startswith(("resume", "checkpoint", "history")):
                scan.pop(name)
        semantics["scan"] = scan
    # Optional v3.3 policy sections are included without depending on loader changes.
    for name in ("staged", "stability", "revalidation"):
        if getattr(config, name, None) is not None:
            semantics[name] = _mapping(getattr(config, name))
    content = json.dumps(semantics, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class CheckpointStore:
    def __init__(self, folder: Path, *, mode: str, network: str, fingerprint: str, max_age_seconds: float):
        self.mode, self.network = _mode(mode), _network(network)
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("Invalid scan configuration fingerprint")
        self.fingerprint = fingerprint
        self.max_age_seconds = _number(max_age_seconds, "max_age_seconds", minimum=1)
        self.path = Path(folder) / f"checkpoint-{self.mode}-{self.network}.json"
        self._created_at = None

    def save(self, candidates: list, state: dict) -> None:
        if not isinstance(candidates, list) or len(candidates) > MAX_CANDIDATES:
            raise ValueError("Invalid checkpoint candidates (maximum 100000)")
        if not isinstance(state, dict):
            raise ValueError("Checkpoint state must be an object")
        now = time.time()
        # A long ALL scan must keep saving. Evidence age is enforced on resume.
        _json_tree(state, now=now)
        rows = [serialize_proxy(proxy) for proxy in candidates]
        if any((row["kind"] == "web") != (self.mode == "web") for row in rows):
            raise ValueError("Checkpoint candidate kind does not match scan mode")
        if len({proxy.key() for proxy in candidates}) != len(candidates):
            raise ValueError("Checkpoint contains duplicate candidates")
        created_at = self._created_at if self._created_at is not None else now
        _timestamp(created_at, "created_at", now=now)
        _write(
            self.path,
            {
                "schema": SCHEMA,
                "mode": self.mode,
                "network": self.network,
                "fingerprint": self.fingerprint,
                "created_at": created_at,
                "updated_at": now,
                "complete": False,
                "candidates": rows,
                "state": state,
            },
        )
        self._created_at = created_at

    def _load(self, *, enforce_age=True):
        row = _read(self.path)
        if row.get("schema") != SCHEMA:
            raise ValueError("Unsupported checkpoint schema; start a fresh scan")
        if row.get("mode") != self.mode or row.get("network") != self.network:
            raise ValueError("Checkpoint mode/network mismatch; start a fresh scan")
        if row.get("fingerprint") != self.fingerprint:
            raise ValueError("Checkpoint configuration changed; start a fresh scan")
        now = time.time()
        max_age = self.max_age_seconds if enforce_age else None
        _json_tree(row, now=now, max_age=max_age)
        created = _timestamp(row.get("created_at"), "created_at", now=now, max_age=max_age)
        updated = _timestamp(row.get("updated_at"), "updated_at", now=now, max_age=max_age)
        if updated < created or type(row.get("complete")) is not bool:
            raise ValueError("Invalid checkpoint timestamps or completion flag")
        if row["complete"]:
            raise ValueError("Saved scan is already complete; start a fresh scan")
        candidates, state = row.get("candidates"), row.get("state")
        if (
            not isinstance(candidates, list)
            or len(candidates) > MAX_CANDIDATES
            or not isinstance(state, dict)
        ):
            raise ValueError("Invalid checkpoint candidates/state")
        candidates = [deserialize_proxy(proxy) for proxy in candidates]
        if any(isinstance(proxy, WebProxy) != (self.mode == "web") for proxy in candidates):
            raise ValueError("Checkpoint candidate kind does not match scan mode")
        if len({proxy.key() for proxy in candidates}) != len(candidates):
            raise ValueError("Checkpoint contains duplicate candidates")
        self._created_at = created
        return candidates, state, row

    def load(self) -> tuple[list, dict]:
        candidates, state, _ = self._load()
        return candidates, state

    def complete(self) -> None:
        # Long active scans can finish without consuming stale proof as a new scan.
        _, _, row = self._load(enforce_age=False)
        row["complete"], row["updated_at"] = True, time.time()
        _write(self.path, row)


class HistoryStore:
    """Compact evidence history; this data never qualifies a live recommendation."""

    def __init__(self, folder: Path, network: str, mode: str, max_runs: int = 1000):
        self.network, self.mode = _network(network), _mode(mode)
        if type(max_runs) is not int or not 1 <= max_runs <= 1000:
            raise ValueError("max_runs must be between 1 and 1000")
        self.max_runs = max_runs
        self.path = Path(folder) / f"history-{self.mode}-{self.network}.json"

    def _runs(self):
        if not self.path.exists():
            return []
        row = _read(self.path)
        if row.get("schema") != SCHEMA or row.get("mode") != self.mode or row.get("network") != self.network:
            raise ValueError("Invalid history schema or mode/network mismatch")
        _json_tree(row, now=time.time())
        runs = row.get("runs")
        if not isinstance(runs, list) or len(runs) > 1000:
            raise ValueError("Invalid history run list")
        observed = 0
        identifiers = set()
        for run in runs:
            if (
                not isinstance(run, dict)
                or not isinstance(run.get("rows"), list)
                or not isinstance(run.get("summary"), dict)
            ):
                raise ValueError("Invalid history run")
            run_id = _text(run.get("run_id"), "history run_id", 128)
            if run_id in identifiers:
                raise ValueError("Invalid duplicate history run")
            identifiers.add(run_id)
            _timestamp(run.get("updated_at"), "history updated_at")
            observed += len(run["rows"])
            identities = set()
            for item in run["rows"]:
                self._validate_row(item)
                if item["identity"] in identities:
                    raise ValueError("Invalid duplicate history observation")
                identities.add(item["identity"])
        if observed > MAX_HISTORY_OBSERVATIONS:
            raise ValueError("History exceeds observation safety limit")
        return runs

    def _validate_row(self, row):
        if not isinstance(row, dict) or not re.fullmatch(r"[a-f0-9]{64}", str(row.get("identity", ""))):
            raise ValueError("Invalid history identity")
        normalize_server(row.get("server"))
        normalize_port(row.get("port"))
        kind = row.get("kind")
        if (
            not isinstance(kind, str)
            or kind not in {"mtproto", "web"}
            or type(row.get("qualified")) is not bool
        ):
            raise ValueError("Invalid history proxy kind or qualified flag")
        if (kind == "web") != (self.mode == "web"):
            raise ValueError("History proxy kind does not match mode")
        if (
            row.get("protocol") not in ("http", "https", "socks4", "socks5")
            and row.get("protocol") != "mtproto"
        ):
            raise ValueError("Invalid history protocol")
        for name in ("successes", "attempts"):
            if type(row.get(name)) is not int or not 0 <= row[name] <= MAX_SAMPLES:
                raise ValueError(f"Invalid history {name}")
        if row["successes"] > row["attempts"]:
            raise ValueError("Invalid history success count")
        if row.get("p95_ms") is not None:
            _number(row["p95_ms"], "history p95")
        _timestamp(row.get("last_tested"), "history last_tested")

    def record(self, run_id: str, results: list, summary: dict) -> None:
        _text(run_id, "history run_id", 128)
        if not isinstance(results, list) or len(results) > MAX_CANDIDATES or not isinstance(summary, dict):
            raise ValueError("Invalid history results/summary")
        now = time.time()
        _json_tree(summary, now=now)
        rows = []
        seen = set()
        for result in results:
            validated = deserialize_result(serialize_result(result))
            proxy = validated.proxy
            if isinstance(proxy, WebProxy) != (self.mode == "web"):
                raise ValueError("History candidate kind does not match scan mode")
            identity = hashlib.sha256(proxy.key().encode("utf-8")).hexdigest()
            if identity in seen:
                raise ValueError("History contains duplicate candidates")
            seen.add(identity)
            if not validated.attempts:
                continue  # Untested candidates do not have a last-tested time.
            latencies = sorted(
                sample.total_latency_ms
                for sample in validated.samples
                if sample.success and sample.total_latency_ms is not None
            )
            web_p95 = latencies[math.ceil(0.95 * len(latencies)) - 1] if latencies else None
            rows.append(
                {
                    "identity": identity,
                    "server": proxy.server,
                    "port": proxy.port,
                    "kind": "web" if isinstance(proxy, WebProxy) else "mtproto",
                    "protocol": getattr(proxy, "protocol", "mtproto"),
                    "last_tested": max((sample.timestamp for sample in validated.samples), default=now),
                    "successes": validated.successes,
                    "attempts": validated.attempts,
                    "p95_ms": (validated.telegram_p95_ms if isinstance(proxy, Proxy) else web_p95),
                    "qualified": (
                        validated.strict_eligible if isinstance(proxy, Proxy) else validated.successes > 0
                    ),
                }
            )
        runs = [run for run in self._runs() if run["run_id"] != run_id]
        runs.append({"run_id": run_id, "updated_at": now, "summary": summary, "rows": rows})
        runs = runs[-self.max_runs :]
        while len(runs) > 1 and sum(len(run["rows"]) for run in runs) > MAX_HISTORY_OBSERVATIONS:
            runs.pop(0)
        _write(self.path, {"schema": SCHEMA, "mode": self.mode, "network": self.network, "runs": runs})

    def rows(self, limit: int = 20) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= MAX_CANDIDATES:
            raise ValueError("History limit must be between 1 and 100000")
        aggregate = {}
        for run in self._runs():
            for observation in run["rows"]:
                identity = observation["identity"]
                previous = aggregate.get(identity, {})
                current = observation["last_tested"] >= previous.get("last_tested", 0)
                aggregate[identity] = {
                    **(observation if current else previous),
                    "network": self.network,
                    "mode": self.mode,
                    "current_run_id": run["run_id"] if current else previous["current_run_id"],
                    "run_count": previous.get("run_count", 0) + 1,
                    "historical_successes": previous.get("historical_successes", 0)
                    + observation["successes"],
                    "historical_attempts": previous.get("historical_attempts", 0) + observation["attempts"],
                }
        return sorted(aggregate.values(), key=lambda row: (-row["last_tested"], row["identity"]))[:limit]
