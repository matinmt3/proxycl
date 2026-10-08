"""Shared data models for the Telegram MTProto Smart Selector."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlencode

if TYPE_CHECKING:
    from webproxy.models import WebProxy


class FailureReason(str, Enum):
    NONE = "none"
    DNS_ERROR = "dns_error"
    TCP_TIMEOUT = "tcp_timeout"
    TCP_REFUSED = "tcp_refused"
    HANDSHAKE_TIMEOUT = "handshake_timeout"
    HANDSHAKE_INVALID = "handshake_invalid"
    SECRET_INVALID = "secret_invalid"
    UNSUPPORTED_TRANSPORT = "unsupported_transport"
    UNKNOWN = "unknown"


@dataclass
class Proxy:
    """A single MTProto proxy candidate."""

    server: str
    port: int
    secret: str
    source: str = "unknown"
    tag: Optional[str] = None

    def __post_init__(self):
        self.server = self.server.strip().strip("[]").lower()
        self.secret = self.secret.strip()
        # Hexadecimal secrets are case-insensitive. Base64url secrets are not.
        if re.fullmatch(r"[0-9a-fA-F]+", self.secret):
            self.secret = self.secret.lower()

    def key(self) -> str:
        return f"{self.server}:{self.port}:{self.secret}"

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "port": self.port,
            "secret": self.secret,
            "source": self.source,
            "tag": self.tag,
        }

    def tg_link(self) -> str:
        return "tg://proxy?" + urlencode({"server": self.server, "port": self.port, "secret": self.secret})

    def web_link(self) -> str:
        """Telegram's HTTPS share-link flavor of the same MTProto endpoint."""
        return "https://t.me/proxy?" + urlencode(
            {"server": self.server, "port": self.port, "secret": self.secret}
        )


@dataclass
class TestSample:
    """Result of a single connection attempt to a proxy."""

    __test__ = False

    success: bool
    tcp_latency_ms: Optional[float] = None
    handshake_latency_ms: Optional[float] = None
    total_latency_ms: Optional[float] = None
    failure_reason: FailureReason = FailureReason.NONE
    timestamp: float = field(default_factory=time.time)


@dataclass
class ProxyResult:
    """Aggregated benchmark result for a proxy across multiple samples."""

    proxy: Proxy | WebProxy
    samples: list = field(default_factory=list)  # list[TestSample]

    @property
    def attempts(self) -> int:
        return len(self.samples)

    @property
    def successes(self) -> int:
        return sum(1 for s in self.samples if s.success)

    @property
    def success_rate(self) -> float:
        if not self.samples:
            return 0.0
        return self.successes / len(self.samples)

    @property
    def timeout_rate(self) -> float:
        if not self.samples:
            return 0.0
        timeouts = sum(
            1
            for s in self.samples
            if s.failure_reason in (FailureReason.TCP_TIMEOUT, FailureReason.HANDSHAKE_TIMEOUT)
        )
        return timeouts / len(self.samples)

    def _latencies(self) -> list:
        return [
            s.total_latency_ms
            for s in self.samples
            if s.success
            and isinstance(s.total_latency_ms, (int, float))
            and math.isfinite(s.total_latency_ms)
            and s.total_latency_ms >= 0
        ]

    @property
    def avg_latency_ms(self) -> Optional[float]:
        lats = self._latencies()
        if not lats:
            return None
        return sum(lats) / len(lats)

    @property
    def median_latency_ms(self) -> Optional[float]:
        lats = sorted(self._latencies())
        if not lats:
            return None
        n = len(lats)
        mid = n // 2
        if n % 2 == 0:
            return (lats[mid - 1] + lats[mid]) / 2
        return lats[mid]

    @property
    def jitter_ms(self) -> Optional[float]:
        lats = self._latencies()
        if len(lats) < 2:
            return 0.0 if lats else None
        diffs = [abs(lats[i] - lats[i - 1]) for i in range(1, len(lats))]
        return sum(diffs) / len(diffs)

    @property
    def stability(self) -> float:
        """0..1 score: 1 - coefficient of variation, clamped."""
        lats = self._latencies()
        if len(lats) < 2:
            return 1.0 if lats else 0.0
        avg = sum(lats) / len(lats)
        if avg == 0:
            return 0.0
        variance = sum((x - avg) ** 2 for x in lats) / len(lats)
        std = variance**0.5
        cv = std / avg
        return max(0.0, min(1.0, 1.0 - cv))

    @property
    def last_failure_reason(self) -> FailureReason:
        for s in reversed(self.samples):
            if not s.success:
                return s.failure_reason
        return FailureReason.NONE

    score: float = 0.0

    def to_dict(self) -> dict:
        d = self.proxy.to_dict()
        d.update(
            {
                "attempts": self.attempts,
                "successes": self.successes,
                "success_rate": round(self.success_rate, 4),
                "timeout_rate": round(self.timeout_rate, 4),
                "avg_latency_ms": round(self.avg_latency_ms, 2) if self.avg_latency_ms is not None else None,
                "median_latency_ms": (
                    round(self.median_latency_ms, 2) if self.median_latency_ms is not None else None
                ),
                "jitter_ms": round(self.jitter_ms, 2) if self.jitter_ms is not None else None,
                "stability": round(self.stability, 4),
                "score": round(self.score, 4),
                "last_failure_reason": self.last_failure_reason.value,
            }
        )
        if isinstance(self.proxy, Proxy):
            d["tg_link"] = self.proxy.tg_link()
            d["web_link"] = self.proxy.web_link()
        else:
            d["uri"] = self.proxy.uri()
        return d
