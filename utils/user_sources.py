"""Bounded, atomic personal sources, separate from shipped configuration."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from collector.health import source_identity
from config.loader import SourceConfig, WebSourceConfig
from utils.process_lock import OutputRootLock

MODES = {"mtproto": "sources", "telegram": "sources", "web": "web_sources", "webproxy": "webproxy_sources"}
MAX_BYTES = 4 * 1024 * 1024
MAX_SOURCES = 256


def canonical_mode(mode):
    if mode not in MODES:
        raise ValueError("Unknown source family")
    return "mtproto" if mode == "telegram" else mode


def data_dir(config):
    override = getattr(config, "_user_data_dir", None)
    return (
        Path(override).expanduser().resolve()
        if override
        else Path(__file__).resolve().parent.parent / ".revmamad" / "user-data"
    )


def read_document(path, empty):
    try:
        with path.open("rb") as handle:
            body = handle.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return copy.deepcopy(empty)
    if len(body) > MAX_BYTES:
        raise ValueError("Personal data exceeds the size limit")
    try:
        return json.loads(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Personal data is damaged; restore the saved backup") from exc


def atomic_document(path, document):
    body = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(body) > MAX_BYTES:
        raise ValueError("Personal data exceeds the size limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".personal-", delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def _source(row):
    if not isinstance(row, dict) or set(row) - {
        "mode",
        "name",
        "type",
        "url",
        "protocol",
        "enabled",
        "manual",
    }:
        raise ValueError("Invalid personal source")
    mode = canonical_mode(row.get("mode"))
    name = row.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 for c in name):
        raise ValueError("Source name must contain 1–120 readable characters")
    enabled = row.get("enabled", True)
    if type(enabled) is not bool:
        raise ValueError("Source enabled must be true or false")
    kind = row.get("type", "http_txt")
    if kind not in {"http_txt", "http_json", "json_feed", "github_raw", "telegram_channel"}:
        raise ValueError("Unsupported source parser")
    url = row.get("url", "")
    if "manual" in row:
        if url or not isinstance(row["manual"], str) or len(row["manual"].encode("utf-8")) > 1024 * 1024:
            raise ValueError("Invalid manual source contents")
    else:
        if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
            raise ValueError("Invalid feed URL")
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
        ):
            raise ValueError("Use a public HTTP(S) feed URL without embedded credentials or fragments")
    protocol = row.get("protocol", "http")
    if protocol not in {"http", "https", "socks4", "socks5"}:
        raise ValueError("Unsupported HTTP/SOCKS transport")
    result = {"mode": mode, "name": name.strip(), "type": kind, "url": url, "enabled": enabled}
    if mode != "web" and protocol != "http":
        raise ValueError("HTTP/SOCKS protocol is only valid for web sources")
    if mode == "web":
        result["protocol"] = protocol
    if "manual" in row:
        if mode == "web":
            from webproxy.sources import parse_text_blob
        elif mode == "webproxy":
            from collector.telegram_webproxy import parse_feed as parse_text_blob
        else:
            from collector.sources import parse_text_blob
        if not parse_text_blob(row["manual"], name):
            raise ValueError("No valid links for this source family")
        result["manual"] = row["manual"]
    return result


def _document(value):
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "sources", "overrides"}
        or value["version"] != 1
    ):
        raise ValueError("Unsupported personal sources document")
    if not isinstance(value["sources"], list) or len(value["sources"]) > MAX_SOURCES:
        raise ValueError("Too many personal sources")
    overrides = value["overrides"]
    if (
        not isinstance(overrides, dict)
        or len(overrides) > 4096
        or any(
            not re.fullmatch(r"[a-f0-9]{64}", key) or type(enabled) is not bool
            for key, enabled in overrides.items()
        )
    ):
        raise ValueError("Invalid source enable overrides")
    return {"version": 1, "sources": [_source(row) for row in value["sources"]], "overrides": dict(overrides)}


EMPTY = {"version": 1, "sources": [], "overrides": {}}


class SourceStore:
    def __init__(self, config):
        self.config = config
        self.path = data_dir(config) / "sources.json"

    def export_document(self):
        return _document(read_document(self.path, EMPTY))

    def import_document(self, document):
        validated = _document(document)
        with OutputRootLock(self.config.output.folder, operation="sources"):
            if self.path.exists():
                atomic_document(self.path.with_suffix(".backup.json"), self.export_document())
            atomic_document(self.path, validated)
        return len(validated["sources"])

    def add_feed(self, mode, name, url, type="http_txt", protocol="http"):
        return self._add({"mode": mode, "name": name, "url": url, "type": type, "protocol": protocol})

    def import_manual(self, mode, text, name="Personal links"):
        mode = canonical_mode(mode)
        if mode == "web":
            from webproxy.sources import parse_text_blob
        elif mode == "webproxy":
            from collector.telegram_webproxy import parse_feed as parse_text_blob
        else:
            from collector.sources import parse_text_blob
        if not isinstance(text, str) or len(text.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Manual links exceed the size limit")
        proxies = parse_text_blob(text, name)
        if not proxies:
            raise ValueError("No valid links for this source family")
        # Store normalized lines, never arbitrary local file references.
        lines = [p.uri() if mode == "web" else p.tg_link() for p in proxies]
        return self._add({"mode": mode, "name": name, "type": "http_txt", "manual": "\n".join(lines) + "\n"})

    def _add(self, row):
        row = _source(row)
        with OutputRootLock(self.config.output.folder, operation="sources"):
            document = self.export_document()
            if row in document["sources"]:
                return False
            document["sources"].append(row)
            atomic_document(self.path, _document(document))
        return True

    def set_enabled(self, identity, enabled):
        if (
            type(enabled) is not bool
            or not isinstance(identity, str)
            or not re.fullmatch(r"[0-9a-f]{64}", identity)
        ):
            raise ValueError("Invalid source identity or enabled state")
        if identity not in {row["id"] for row in self.list()}:
            raise ValueError("Unknown source identity")
        with OutputRootLock(self.config.output.folder, operation="sources"):
            document = self.export_document()
            document["overrides"][identity] = enabled
            atomic_document(self.path, _document(document))

    def list(self, mode=None):
        config = apply_user_sources(self.config)
        result = []
        families = [canonical_mode(mode)] if mode else ["mtproto", "webproxy", "web"]
        from collector.health import _safe_name

        for family in families:
            for source in getattr(config, MODES[family]):
                result.append(
                    {
                        "id": source_identity(source),
                        "mode": family,
                        "name": _safe_name(source),
                        "enabled": source.enabled,
                        "personal": bool(getattr(source, "_personal", False)),
                        "type": source.type,
                    }
                )
        return result


def apply_user_sources(config):
    """Overlay without editing shipped/custom config or dropping old manual files."""
    updated = copy.copy(config)
    document = SourceStore(config).export_document()
    for field in set(MODES.values()):
        setattr(
            updated,
            field,
            [copy.copy(s) for s in getattr(updated, field) if not getattr(s, "_personal", False)],
        )
    for row in document["sources"]:
        family = row["mode"]
        cls = WebSourceConfig if family == "web" else SourceConfig
        url = row["url"]
        if "manual" in row:
            import hashlib

            identity = hashlib.sha256((family + "\n" + row["manual"]).encode()).hexdigest()
            url = "manual://" + identity
        kwargs = {key: row[key] for key in ("name", "type", "enabled")}
        if family == "web":
            kwargs["protocol"] = row["protocol"]
        source = cls(url=url, **kwargs)
        source._personal = True
        if "manual" in row:
            source._manual_text = row["manual"]
        getattr(updated, MODES[family]).append(source)
    for field in set(MODES.values()):
        for source in getattr(updated, field):
            identity = source_identity(source)
            if identity in document["overrides"]:
                source.enabled = document["overrides"][identity]
    return updated
