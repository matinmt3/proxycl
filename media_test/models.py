"""Bounded settings and public-message validation for the optional media test."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MIB = 1024 * 1024
CHUNK_BYTES = 512 * 1024
TELETHON_VERSION = "1.45.0"
REPORT_SCHEMA = 1
MEDIA_POLICY = "media-prefix-v1-experimental"


class MediaTestError(Exception):
    """Safe categorical errors; never include remote messages or account data."""


class BudgetExceeded(ConnectionError):
    pass


class CdnUnsupported(MediaTestError):
    pass


class FileUnavailable(MediaTestError):
    pass


class EvidenceExpired(MediaTestError):
    pass


@dataclass(frozen=True)
class MediaSettings:
    max_candidates: int = 5
    rounds: int = 2
    round_bytes: int = 8 * MIB
    budget_bytes: int = 100 * MIB
    timeout_seconds: float = 600.0
    proof_max_age_seconds: float = 1800.0
    stall_seconds: float = 2.0

    def __post_init__(self):
        for name, low, high in (
            ("max_candidates", 1, 5),
            ("rounds", 2, 2),
            ("round_bytes", CHUNK_BYTES, 8 * MIB),
            ("budget_bytes", MIB, 100 * MIB),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"Invalid media setting: {name}")
        if self.round_bytes % CHUNK_BYTES:
            raise ValueError("Media round size must be a multiple of 512 KiB")
        for name, low, high in (
            ("timeout_seconds", 1, 600),
            ("proof_max_age_seconds", 1, 1800),
            ("stall_seconds", 0.1, 60),
        ):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"Invalid media setting: {name}")


def parse_public_message_link(link: str) -> tuple[str, int]:
    """Only public, explicit channel post URLs; no invites, private chats or queries."""
    if not isinstance(link, str) or len(link) > 300:
        raise ValueError("Provide one public Telegram message URL")
    parsed = urlsplit(link)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "t.me"
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Za-z][A-Za-z0-9_]{3,31}/[1-9][0-9]{0,9}", parsed.path)
    ):
        raise ValueError("Expected https://t.me/PUBLIC_CHANNEL/MESSAGE_NUMBER")
    channel, message_id = parsed.path.strip("/").split("/")
    if int(message_id) > 2**31 - 1:
        raise ValueError("Telegram message number exceeds the protocol range")
    if channel.lower() in {"joinchat", "proxy", "socks", "webproxy", "share", "login"}:
        raise ValueError("Expected a public channel message")
    return channel, int(message_id)
