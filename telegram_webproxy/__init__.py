"""Telegram's WEB relay protocol, distinct from generic HTTP/SOCKS proxies.

The carrier is a native Python TLS client. It verifies the relay certificate but
does not reproduce a Telegram WebView's TLS fingerprint or execute bridge scripts.
"""

from telegram_webproxy.models import TelegramWebProxy
from telegram_webproxy.parser import extract_links, parse_line

__all__ = ["TelegramWebProxy", "extract_links", "parse_line"]
