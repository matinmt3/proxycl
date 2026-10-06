"""Parsers for Telegram proxy links and text/JSON feeds."""

from __future__ import annotations

import html
import json
import re
from urllib.parse import parse_qs, urlparse

from tester.secrets import decode_secret, normalize_port, normalize_server
from utils.models import Proxy

TG_LINK_RE = re.compile(r"(?:https?://(?:t\.me|telegram\.me)/proxy|tg://proxy)\?[^\s\"'<>]+", re.IGNORECASE)


def _make_proxy(server: object, port: object, secret: object, source: str) -> Proxy | None:
    try:
        if not isinstance(server, str) or not isinstance(secret, str):
            return None
        return Proxy(
            server=normalize_server(server),
            port=normalize_port(port),
            secret=decode_secret(secret).encoded,
            source=source,
        )
    except (TypeError, ValueError):
        return None


def _proxy_from_query(url: str, source: str) -> Proxy | None:
    try:
        qs = parse_qs(urlparse(url.rstrip(").,;")).query)
        if any(len(qs.get(key, [])) != 1 for key in ("server", "port", "secret")):
            return None
        return _make_proxy(qs["server"][0], qs["port"][0], qs["secret"][0], source)
    except ValueError:
        return None


def _dedupe(proxies: list[Proxy]) -> list[Proxy]:
    unique = {}
    for proxy in proxies:
        unique.setdefault(proxy.key(), proxy)
    return list(unique.values())


def parse_text_blob(text: str, source: str) -> list[Proxy]:
    """Parse links or server:port:secret lines, including IPv6 and base64 secrets."""
    proxies: list[Proxy] = []
    text = html.unescape(text).lstrip("\ufeff")
    for match in TG_LINK_RE.finditer(text):
        proxy = _proxy_from_query(match.group(0), source)
        if proxy:
            proxies.append(proxy)
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or "://" in line:
            continue
        fields = line.rsplit(":", 2)
        if len(fields) != 3:
            continue
        proxy = _make_proxy(*fields, source)
        if proxy:
            proxies.append(proxy)
    return _dedupe(proxies)


def parse_json_feed(text: str, source: str) -> list[Proxy]:
    """Accept proxy objects, arrays of links, and feeds containing nested string values."""
    try:
        data = json.loads(text.lstrip("\ufeff"))
    except (json.JSONDecodeError, RecursionError):
        return parse_text_blob(text, source)
    proxies: list[Proxy] = []
    pending = [data]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            fields = {key.lower(): item for key, item in value.items() if isinstance(key, str)}
            server = fields.get("server") or fields.get("host") or fields.get("ip")
            proxy = _make_proxy(server, fields.get("port"), fields.get("secret"), source)
            if proxy:
                proxies.append(proxy)
            pending.extend(reversed(list(value.values())))
        elif isinstance(value, list):
            pending.extend(reversed(value))
        elif isinstance(value, str):
            proxies.extend(parse_text_blob(value, source))
    return _dedupe(proxies)
