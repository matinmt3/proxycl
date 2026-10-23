"""Revalidate expired evidence while keeping a bounded, unchanged candidate pool.

A fresh retry cannot rehabilitate an old sample. If even one sample for an
endpoint has expired, all its samples are discarded before the next engine is
constructed. History and candidate ordering are never used as current proof.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from telegram_webproxy.models import TelegramWebProxy
from utils.models import Proxy
from utils.scan_state import (
    REFRESH_RETENTION_SECONDS,
    _json_tree,
    _timestamp,
    deserialize_result,
    serialize_result,
)


def _context_time(value, name, *, now):
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                raise ValueError("Timezone required")
            value = stamp.timestamp()
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid checkpoint {name}") from exc
    return _timestamp(value, name, now=now)


def refresh_saved_evidence(config, candidates: list, state: dict) -> tuple[dict, dict]:
    """Return a refreshed envelope and exact discarded/retained evidence counts.

    Stores must call load_for_refresh first. This helper additionally validates
    result membership and original engine semantics before deleting evidence, so
    a malformed old checkpoint cannot become valid merely by being stale.
    Caller-owned dictionaries, samples, candidate objects and run_id are kept.
    """
    now = time.time()
    ttl = config.scan.checkpoint_max_age_minutes * 60
    if not isinstance(candidates, list) or len(candidates) > 1_000_000:
        raise ValueError("Invalid refresh candidate pool")
    selected = {proxy.key(): proxy for proxy in candidates}
    if len(selected) != len(candidates):
        raise ValueError("Refresh candidate pool contains duplicates")
    if not isinstance(state, dict) or not isinstance(state.get("context"), dict):
        raise ValueError("Checkpoint has no refresh context")
    engine = state.get("engine")
    if (
        not isinstance(engine, dict)
        or type(engine.get("schema")) is not int
        or engine["schema"] != 1
        or not isinstance(engine.get("results"), list)
        or len(engine["results"]) > len(candidates)
    ):
        raise ValueError("Invalid refresh engine")
    context = state["context"]
    _json_tree({**state, "engine": {**engine, "results": []}}, now=now)
    started = _context_time(context.get("started_at"), "started_at", now=now)
    origin_value = context.get(
        "candidate_pool_created_at", context.get("original_started_at", context["started_at"])
    )
    origin = _context_time(origin_value, "candidate_pool_created_at", now=now)
    _timestamp(origin, "candidate pool retention", now=now, max_age=REFRESH_RETENTION_SECONDS)
    if origin > started:
        raise ValueError("Candidate pool origin is newer than the active scan")

    staged = engine.get("stage") in {"prefilter", "baseline", "finalists"} or (
        engine.get("stage") == "complete" and "prefilter" in engine
    )
    if staged:
        from tester.staged import StagedTester

        StagedTester(config, initial_state=engine)._restore(candidates)
    elif engine.get("stage") not in {"testing", "complete"}:
        raise ValueError("Invalid refresh engine stage")

    retained, seen = [], set()
    stale_candidates = stale_samples = retained_samples = 0
    for row in engine["results"]:
        _json_tree(row, now=now)
        result = deserialize_result(row)
        key = result.proxy.key()
        if key not in selected or key in seen or type(result.proxy) is not type(selected[key]):
            raise ValueError("Refresh result is not a unique selected candidate")
        seen.add(key)
        strict = isinstance(result.proxy, (Proxy, TelegramWebProxy))
        expected_profile = (
            "telegram_session" if strict and config.testing.mtproto_profile == "telegram_session" else None
        )
        if expected_profile is not None and result.verification_profile != expected_profile:
            raise ValueError("Refresh result verification profile changed")
        maximum = config.testing.retries + (config.scan.finalist_retries if staged else 0)
        if result.attempts > maximum:
            raise ValueError("Refresh result exceeds the configured attempt count")
        result.proxy = selected[key]
        if any(now - sample.timestamp > ttl for sample in result.samples):
            stale_candidates += 1
            stale_samples += result.attempts
        elif result.samples:
            retained.append(serialize_result(result))
            retained_samples += result.attempts

    old_context = now - started > ttl
    refreshed = bool(stale_candidates or old_context)
    stats = {
        "refreshed": refreshed,
        "stale_candidates": stale_candidates,
        "stale_samples": stale_samples,
        "candidates_retested": stale_candidates,
        "retained_candidates": len(retained),
        "retained_samples": retained_samples,
        "screening_reset": old_context,
    }
    if not refreshed:
        return {**state, "context": dict(context), "engine": dict(engine)}, stats

    changed_engine = {**engine, "results": retained}
    if staged:
        changed_engine.update(
            stage="prefilter" if config.scan.prefilter else "baseline",
            prefilter={},
            finalist_keys=[],
            finalist_attempts={},
            completed_finalists={},
            coverage_limited=True,
            budget_round_finished=False,
        )
        for row in retained:
            result = deserialize_result(row)
            key = result.proxy.key()
            if config.scan.prefilter:
                changed_engine["prefilter"][key] = {"passed": True, "reason": "none"}
            extra = result.attempts - config.testing.retries
            if extra > 0:
                changed_engine["completed_finalists"][key] = extra
    else:
        changed_engine["stage"] = "testing"
    stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
    changed_context = {
        **context,
        "candidate_pool_created_at": origin_value,
        "original_started_at": context.get("original_started_at", context["started_at"]),
        "started_at": stamp,
    }
    stats["screening_reset"] = bool(staged and engine.get("prefilter"))
    return {**state, "context": changed_context, "engine": changed_engine}, stats
