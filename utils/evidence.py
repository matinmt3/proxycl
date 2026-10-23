"""Canonical freshness checks for saved proof, independent of UI and transports."""

from __future__ import annotations

import math
import time

TIME_FIELDS = ("oldest_observed_at", "latest_observed_at", "proof_max_age_seconds", "evidence_expires_at")


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def saved_evidence_status(
    row: dict, *, now: float | None = None, max_age_seconds: float | None = None
) -> str:
    """Return missing/invalid/expired/fresh; never trust a saved freshness flag.

    v3.4 exports lacking dated proof remain readable but need a fresh test before
    presenting a usable link. An expiry alone cannot establish observation time.
    """
    if not isinstance(row, dict):
        return "invalid"
    values = [row.get(key) for key in TIME_FIELDS]
    if all(value is None for value in values):
        return "missing"
    if not all(_finite(value) and value > 0 for value in values):
        return "invalid"
    oldest, latest, ttl, expiry = values
    now = time.time() if now is None else now
    if (
        not _finite(now)
        or not 60 <= ttl <= 86400
        or not oldest <= latest <= now + 60
        or not math.isclose(expiry, oldest + ttl, rel_tol=0, abs_tol=0.001)
        or row.get("report_schema", 2) != 2
    ):
        return "invalid"
    if max_age_seconds is not None:
        if not _finite(max_age_seconds) or max_age_seconds <= 0:
            return "invalid"
        expiry = min(expiry, oldest + max_age_seconds)
    return "fresh" if now < expiry else "expired"
