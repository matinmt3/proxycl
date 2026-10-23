"""Parse only Telegram WEB links; t.me/proxy remains ordinary MTProxy."""

from __future__ import annotations

import html
import re
from urllib.parse import parse_qs, urlsplit

from telegram_webproxy.models import TelegramWebProxy, decode_web_secret, normalize_address

_LINK = re.compile(r"(?:tg://webproxy\?|https://(?:t\.me|telegram\.me)/webproxy\?)[^\s<>\"'`]+", re.I)


def parse_line(value: str, source: str = "unknown") -> TelegramWebProxy | None:
    if not isinstance(value, str) or len(value) > 4096:
        return None
    try:
        value = html.unescape(value.strip())
        url = urlsplit(value)
        is_tg = url.scheme.lower() == "tg" and url.netloc.lower() == "webproxy" and not url.path
        is_web = (
            url.scheme.lower() == "https"
            and url.netloc.lower() in ("t.me", "telegram.me")
            and url.path.lower() == "/webproxy"
        )
        if not (is_tg or is_web) or url.fragment:
            return None
        # Bad percent escapes must not survive parse_qs's permissive decoding.
        if re.search(r"%(?![0-9A-Fa-f]{2})", url.query):
            return None
        query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True, max_num_fields=8)
        if set(query) != {"server", "secret"} or any(len(items) != 1 for items in query.values()):
            return None
        server = query["server"][0]
        _, path = normalize_address(server)
        secret = decode_web_secret(query["secret"][0], path=bool(path))
        return TelegramWebProxy(server, secret, source=source)
    except (ValueError, UnicodeError):
        return None


def extract_links(text: str, source: str = "unknown") -> list[TelegramWebProxy]:
    if not isinstance(text, str):
        return []
    found = {}
    for match in _LINK.finditer(html.unescape(text)):
        proxy = parse_line(match.group().rstrip(").,;]"), source)
        if proxy is not None:
            found.setdefault(proxy.key(), proxy)
    return list(found.values())
