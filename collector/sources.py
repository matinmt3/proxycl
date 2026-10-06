"""Parsers that turn raw source payloads into Proxy objects."""
from __future__ import annotations

import json
import re
from typing import List
from urllib.parse import parse_qs, urlparse

from utils.models import Proxy

TG_LINK_RE = re.compile(
    r"(?:https?://t\.me/proxy|tg://proxy)\?[^\s\"'<>]+", re.IGNORECASE
)
LINE_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+):(\d{2,5}):([A-Fa-f0-9]{16,64})\s*$")


def _proxy_from_query(url: str, source: str) -> Proxy | None:
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    server = qs.get("server", [None])[0]
    port = qs.get("port", [None])[0]
    secret = qs.get("secret", [None])[0]
    if not (server and port and secret):
        return None
    try:
        return Proxy(server=server, port=int(port), secret=secret, source=source)
    except ValueError:
        return None


def parse_text_blob(text: str, source: str) -> List[Proxy]:
    """Parse tg:// links, t.me/proxy links, and server:port:secret lines from raw text."""
    proxies: List[Proxy] = []

    for match in TG_LINK_RE.finditer(text):
        p = _proxy_from_query(match.group(0), source)
        if p:
            proxies.append(p)

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = LINE_RE.match(line)
        if m:
            server, port, secret = m.groups()
            try:
                proxies.append(Proxy(server=server, port=int(port), secret=secret, source=source))
            except ValueError:
                continue

    return proxies


def parse_json_feed(text: str, source: str) -> List[Proxy]:
    """Parse a JSON feed. Supports either a list of proxy dicts, or {"proxies": [...]}.

    Recognized dict keys (case-insensitive-ish): server/host/ip, port, secret.
    Falls back to scanning any string values for tg:// links.
    """
    proxies: List[Proxy] = []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # not valid JSON, fall back to text parsing (some feeds embed links in JS)
        return parse_text_blob(text, source)

    items = data.get("proxies") if isinstance(data, dict) else data
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            server = item.get("server") or item.get("host") or item.get("ip")
            port = item.get("port")
            secret = item.get("secret")
            if server and port and secret:
                try:
                    proxies.append(Proxy(server=str(server), port=int(port), secret=str(secret), source=source))
                except ValueError:
                    continue

    if not proxies:
        # fall back to regex scan of the raw JSON text (covers non-standard schemas)
        proxies = parse_text_blob(text, source)

    return proxies
