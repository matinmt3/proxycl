"""Strict parsers for public endpoint feeds; authenticated endpoints are excluded."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

from webproxy.models import WebProxy


def _make_proxy(server, port, protocol, source) -> WebProxy | None:
    try:
        return WebProxy(server, port, protocol, source)
    except (TypeError, ValueError):
        return None


def parse_text_blob(text: str, source: str, protocol: str = "http") -> list[WebProxy]:
    unique: dict[str, WebProxy] = {}
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or any(c.isspace() for c in line) or "@" in line:
            continue
        try:
            parsed = urlsplit(line if "://" in line else "//" + line)
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                continue
            # Explicit schemes override the source's bare-endpoint transport.
            kind = parsed.scheme or protocol
            if parsed.hostname is None or parsed.port is None:
                continue
            proxy = _make_proxy(parsed.hostname, parsed.port, kind, source)
        except (TypeError, ValueError):
            continue
        if proxy is not None:
            unique.setdefault(proxy.key(), proxy)
    return list(unique.values())


def parse_json_feed(text: str, source: str, protocol: str = "http") -> list[WebProxy]:
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except (ValueError, RecursionError):
        return parse_text_blob(text, source, protocol)
    unique: dict[str, WebProxy] = {}
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            fields = {k.lower(): v for k, v in value.items() if isinstance(k, str)}
            if any(fields.get(k) for k in ("username", "password", "user", "pass", "auth")):
                continue
            proxy = _make_proxy(
                fields.get("server") or fields.get("host") or fields.get("ip"),
                fields.get("port"),
                fields.get("protocol", protocol),
                source,
            )
            if proxy is not None:
                unique.setdefault(proxy.key(), proxy)
            pending.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            pending.extend(reversed(value))
        elif isinstance(value, str):
            for proxy in parse_text_blob(value, source, protocol):
                unique.setdefault(proxy.key(), proxy)
    return list(unique.values())
