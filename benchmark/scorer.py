"""Computes a weighted 0..1 score for each proxy from its benchmark samples."""
from __future__ import annotations

from typing import List

from config.loader import ScoringConfig
from utils.models import ProxyResult


def _latency_score(avg_latency_ms: float | None, max_latency_ms: float) -> float:
    if avg_latency_ms is None:
        return 0.0
    if avg_latency_ms >= max_latency_ms:
        return 0.0
    return max(0.0, 1.0 - (avg_latency_ms / max_latency_ms))


def score_result(result: ProxyResult, scoring: ScoringConfig) -> float:
    cfg = scoring.normalized()

    latency_component = _latency_score(result.avg_latency_ms, cfg.max_latency_ms)
    success_component = result.success_rate
    stability_component = result.stability
    timeout_component = 1.0 - result.timeout_rate

    score = (
        latency_component * cfg.latency_weight
        + success_component * cfg.success_rate_weight
        + stability_component * cfg.stability_weight
        + timeout_component * cfg.timeout_weight
    )
    return max(0.0, min(1.0, score))


def score_all(results: List[ProxyResult], scoring: ScoringConfig) -> List[ProxyResult]:
    for r in results:
        r.score = score_result(r, scoring)
    return results


def rank(results: List[ProxyResult]) -> List[ProxyResult]:
    return sorted(results, key=lambda r: r.score, reverse=True)
