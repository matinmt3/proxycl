"""Shared data models for the Telegram MTProto Smart Selector."""

from __future__ import annotations

import math
import re
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlencode

from telegram_webproxy.models import TelegramWebProxy
from utils.evidence import _finite, saved_evidence_status

if TYPE_CHECKING:
    from webproxy.models import WebProxy


class FailureReason(str, Enum):
    NONE = "none"
    DNS_ERROR = "dns_error"
    TLS_ERROR = "tls_error"
    CLIENT_INCOMPATIBLE = "client_incompatible"
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
    source_memberships: list[str] = field(default_factory=list)

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
            "source_memberships": list(self.source_memberships),
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
    verification_level: str = "transport"
    dc_id: Optional[int] = None
    ping_rtts_ms: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class TelegramQualityPolicy:
    """Minimum proof for usable Telegram links; never inferred from a low score filter."""

    min_successes: int = 3
    min_success_rate: float = 0.8
    required_dc_ids: tuple[int, ...] = (2, 4)
    min_ping_count: int = 3
    max_p95_ms: float = 1500.0
    max_jitter_ms: float = 500.0

    def __post_init__(self):
        if type(self.min_successes) is not int or self.min_successes < 3:
            raise ValueError("min_successes must be at least 3")
        if type(self.min_ping_count) is not int or self.min_ping_count < 3:
            raise ValueError("min_ping_count must be at least 3")
        for name in ("min_success_rate", "max_p95_ms", "max_jitter_ms"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Invalid Telegram policy {name}")
        if not 0.8 <= self.min_success_rate <= 1:
            raise ValueError("min_success_rate must be between 0.8 and 1")
        ids = self.required_dc_ids
        if (
            not isinstance(ids, (list, tuple))
            or len(ids) < 2
            or any(type(dc) is not int or not 1 <= dc <= 5 for dc in ids)
            or len(set(ids)) != len(ids)
        ):
            raise ValueError("required_dc_ids must contain at least two distinct Telegram DC IDs")
        object.__setattr__(self, "required_dc_ids", tuple(ids))

    def to_dict(self) -> dict:
        return {
            "min_successes": self.min_successes,
            "min_success_rate": self.min_success_rate,
            "required_dc_ids": list(self.required_dc_ids),
            "min_ping_count": self.min_ping_count,
            "max_p95_ms": self.max_p95_ms,
            "max_jitter_ms": self.max_jitter_ms,
        }


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
        if self.verification_profile == "telegram_session":
            return self.strict_successes
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
        samples = self._strict_samples() if self.verification_profile == "telegram_session" else self.samples
        return [
            s.total_latency_ms
            for s in samples
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
        if self.verification_profile == "telegram_session":
            if self.strict_successes < 2 or self.telegram_jitter_ms is None:
                return 0.0
            return max(0.0, 1.0 - self.telegram_jitter_ms / self.quality_policy.max_jitter_ms)
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
    verification_profile: str = "transport"
    quality_policy: TelegramQualityPolicy = field(default_factory=TelegramQualityPolicy)
    ranking_policy: str = "v1"
    proof_max_age_seconds: float = 1800.0
    provisional: bool = False

    @property
    def oldest_observed_at(self) -> Optional[float]:
        values = [s.timestamp for s in self.samples]
        if not values or any(not _finite(v) or v <= 0 for v in values):
            return None
        return min(values)

    @property
    def latest_observed_at(self) -> Optional[float]:
        return max(s.timestamp for s in self.samples) if self.oldest_observed_at is not None else None

    @property
    def evidence_expires_at(self) -> Optional[float]:
        oldest = self.oldest_observed_at
        return (
            oldest + self.proof_max_age_seconds
            if oldest is not None and _finite(self.proof_max_age_seconds)
            else None
        )

    def _evidence_times(self) -> dict:
        return {
            "oldest_observed_at": self.oldest_observed_at,
            "latest_observed_at": self.latest_observed_at,
            "proof_max_age_seconds": self.proof_max_age_seconds,
            "evidence_expires_at": self.evidence_expires_at,
        }

    def evidence_fresh(self, *, now: float | None = None, max_age_seconds: float | None = None) -> bool:
        """All finished evidence must be recent; a new sample cannot renew old proof."""
        return (
            saved_evidence_status(self._evidence_times(), now=now, max_age_seconds=max_age_seconds) == "fresh"
        )

    @property
    def freshness_status(self) -> str:
        if not self.samples:
            return "missing"
        return saved_evidence_status(self._evidence_times())

    @property
    def within_session_jitter_ms(self) -> Optional[float]:
        differences = [
            abs(b - a)
            for sample in self._strict_samples()
            for a, b in zip(sample.ping_rtts_ms, sample.ping_rtts_ms[1:])
        ]
        return statistics.mean(differences) if differences else None

    @property
    def interconnection_drift_ms(self) -> Optional[float]:
        """Population deviation of each independent session's median ping RTT."""
        medians = [statistics.median(sample.ping_rtts_ms) for sample in self._strict_samples()]
        return statistics.pstdev(medians) if len(medians) >= 2 else None

    def _strict_samples(self) -> list[TestSample]:
        if self.verification_profile != "telegram_session" or not isinstance(
            self.proxy, (Proxy, TelegramWebProxy)
        ):
            return []
        return [
            sample
            for sample in self.samples
            if sample.success
            and sample.verification_level == "telegram_session"
            and type(sample.total_latency_ms) in (int, float)
            and math.isfinite(sample.total_latency_ms)
            and sample.total_latency_ms >= 0
            and type(sample.dc_id) is int
            and 1 <= sample.dc_id <= 5
            and isinstance(sample.ping_rtts_ms, (list, tuple))
            and len(sample.ping_rtts_ms) >= self.quality_policy.min_ping_count
            and all(
                type(rtt) in (int, float) and math.isfinite(rtt) and rtt >= 0 for rtt in sample.ping_rtts_ms
            )
        ]

    @property
    def strict_successes(self) -> int:
        return len(self._strict_samples())

    @property
    def dc_ids_verified(self) -> list[int]:
        return sorted({sample.dc_id for sample in self._strict_samples()})

    def _telegram_rtts(self) -> list[float]:
        return [rtt for sample in self._strict_samples() for rtt in sample.ping_rtts_ms]

    @property
    def ping_count(self) -> int:
        return len(self._telegram_rtts())

    @property
    def telegram_median_ms(self) -> Optional[float]:
        values = sorted(self._telegram_rtts())
        if not values:
            return None
        middle = len(values) // 2
        return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2

    @property
    def telegram_p95_ms(self) -> Optional[float]:
        values = sorted(self._telegram_rtts())
        return values[math.ceil(0.95 * len(values)) - 1] if values else None

    @property
    def telegram_jitter_ms(self) -> Optional[float]:
        values = self._telegram_rtts()
        if len(values) < 2:
            return None
        return sum(abs(b - a) for a, b in zip(values, values[1:])) / (len(values) - 1)

    @property
    def reliability_lower_bound(self) -> float:
        """Wilson lower bound (z=1.96), using finished connections, not correlated pings."""
        attempts = self.attempts
        if not attempts:
            return 0.0
        successes = (
            self.strict_successes if self.verification_profile == "telegram_session" else self.successes
        )
        rate = successes / attempts
        z2 = 1.96**2
        numerator = (
            rate
            + z2 / (2 * attempts)
            - 1.96 * math.sqrt((rate * (1 - rate) + z2 / (4 * attempts)) / attempts)
        )
        return max(0.0, min(1.0, numerator / (1 + z2 / attempts)))

    @property
    def strict_eligible(self) -> bool:
        return self.evidence_fresh() and self.quality_eligible

    @property
    def quality_eligible(self) -> bool:
        """Structural quality only, used to validate old checkpoints before refresh."""
        successes = self.strict_successes
        p95, jitter = self.telegram_p95_ms, self.telegram_jitter_ms
        policy = self.quality_policy
        return (
            self.verification_profile == "telegram_session"
            and successes >= policy.min_successes
            and successes / max(1, self.attempts) >= policy.min_success_rate
            and set(policy.required_dc_ids).issubset(self.dc_ids_verified)
            and p95 is not None
            and p95 <= policy.max_p95_ms
            and jitter is not None
            and jitter <= policy.max_jitter_ms
        )

    @property
    def ranking_latency_ms(self) -> Optional[float]:
        return (
            self.telegram_p95_ms if self.verification_profile == "telegram_session" else self.avg_latency_ms
        )

    def to_dict(self, *, now: float | None = None) -> dict:
        # A published row describes one instant, even when serialization crosses
        # its expiry boundary. Readers independently check expiry when used.
        now = time.time() if now is None else now
        evidence_status = (
            saved_evidence_status(self._evidence_times(), now=now) if self.samples else "missing"
        )
        d = self.proxy.to_dict()
        d.update(
            {
                "report_schema": 2,
                "ranking_policy": self.ranking_policy,
                "evidence_type": (
                    self.verification_profile if isinstance(self.proxy, (Proxy, TelegramWebProxy)) else "web"
                ),
                "oldest_observed_at": self.oldest_observed_at,
                "latest_observed_at": self.latest_observed_at,
                "evidence_expires_at": self.evidence_expires_at,
                "proof_max_age_seconds": self.proof_max_age_seconds,
                "evidence_status": evidence_status,
                "provisional": self.provisional,
                "result_status": (
                    "expired"
                    if evidence_status != "fresh"
                    else "provisional" if self.provisional else "complete"
                ),
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
        if isinstance(self.proxy, (Proxy, TelegramWebProxy)):
            d["tg_link"] = self.proxy.tg_link()
            d["web_link"] = self.proxy.web_link()
            d.update(
                {
                    "verification_profile": self.verification_profile,
                    "strict_eligible": evidence_status == "fresh" and self.quality_eligible,
                    "strict_successes": self.strict_successes,
                    "dc_ids_verified": self.dc_ids_verified,
                    "ping_count": self.ping_count,
                    "telegram_median_ms": self.telegram_median_ms,
                    "telegram_p95_ms": self.telegram_p95_ms,
                    "telegram_jitter_ms": self.telegram_jitter_ms,
                    "within_session_jitter_ms": self.within_session_jitter_ms,
                    "interconnection_drift_ms": self.interconnection_drift_ms,
                    "reliability_lower_bound": self.reliability_lower_bound,
                    "strict_policy": self.quality_policy.to_dict(),
                }
            )
        else:
            d["uri"] = self.proxy.uri()
            d["verification_profile"] = "web"
        return d
