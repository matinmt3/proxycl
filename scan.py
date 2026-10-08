"""Independent scan orchestration and completed per-mode snapshots."""

from __future__ import annotations

import math
from collections import Counter, deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from config.loader import AppConfig

MODES = ("mtproto", "web")


def validate_request(mode: str, limit: int | None) -> None:
    if mode not in MODES:
        raise ValueError("mode must be mtproto or web")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("Candidate count must be a positive integer or ALL")


def mode_folder(config: AppConfig, mode: str) -> Path:
    validate_request(mode, None)
    return Path(config.output.folder) / mode


def select_candidates(proxies: list, limit: int | None) -> list:
    """Deduplicate globally, then take deterministic turns between source groups."""
    validate_request("mtproto", limit)
    seen = set()
    groups = {}
    for proxy in proxies:
        key = proxy.key()
        if key in seen:
            continue
        seen.add(key)
        groups.setdefault(proxy.source, deque()).append(proxy)
    queues = deque(groups.values())
    selected = []
    while queues and (limit is None or len(selected) < limit):
        queue = queues.popleft()
        selected.append(queue.popleft())
        if queue:
            queues.append(queue)
    return selected


@dataclass
class ScanOutcome:
    results: list
    eligible: list
    top: list
    summary: dict
    source_reports: list[dict]
    folder: Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def run_scan(
    config: AppConfig, mode: str = "mtproto", limit: int | None = None, export: bool = True
) -> ScanOutcome:
    validate_request(mode, limit)
    from benchmark.scorer import rank, score_all
    from exporter.exporter import Exporter, eligible_results

    if mode == "mtproto":
        from collector.collector import Collector
        from tester.runner import ParallelTester

        collector, tester, exporter_type = Collector(config), ParallelTester(config), Exporter
    else:
        from webproxy.collector import WebCollector
        from webproxy.exporter import WebExporter
        from webproxy.tester import WebTester

        collector, tester, exporter_type = WebCollector(config), WebTester(config), WebExporter
    started = _now()
    print(f"\n[{mode.upper()}] Collecting source feeds...", flush=True)
    candidates = select_candidates(collector.collect(), None)
    selected = select_candidates(candidates, limit)
    requested = "ALL" if limit is None else str(limit)
    print(f"Collected: {len(candidates)} | Selected: {len(selected)} | Request: {requested}", flush=True)
    if selected:
        from tester.runner import worker_count

        workers = worker_count(config.testing.workers, len(selected))
        estimated = (
            math.ceil(len(selected) / workers)
            * config.testing.retries
            * (config.testing.timeout_seconds + config.testing.connect_timeout_seconds)
        )
        print(
            f"Testing {len(selected)} candidates, {workers} workers. Approximate timeout budget: {estimated / 60:.1f} min. Ctrl+C cancels.",
            flush=True,
        )
        results = rank(score_all(tester.run(selected), config.scoring))
    else:
        results = []
    folder = mode_folder(config, mode)
    output_cfg = replace(config.output, folder=str(folder))
    # Filtering does not need to create an output directory in benchmark-only runs.
    eligible = eligible_results(results, output_cfg)
    failures = Counter(s.failure_reason.value for r in results for s in r.samples if not s.success)
    summary = {
        "run_id": str(uuid4()),
        "started_at": started,
        "completed_at": _now(),
        "requested_count": limit,
        "collected": len(candidates),
        "selected": len(selected),
        "tested": len(results),
        "verified": sum(r.successes > 0 and r.avg_latency_ms is not None for r in results),
        "eligible": len(eligible),
        "displayed": min(10, len(eligible)),
        "failure_counts": dict(sorted(failures.items())),
    }
    reports = list(getattr(collector, "source_reports", []))
    if export:
        import json

        exporter = exporter_type(output_cfg)
        exporter.export_all(results)
        if 10 not in output_cfg.top_sizes:
            exporter.export_top_n(eligible, 10)
        exporter.export_best_n_readable(results, n=10)
        snapshot = {
            "version": 1,
            "mode": mode,
            "summary": summary,
            "source_reports": reports,
            "rows": [r.to_dict() for r in eligible],
        }
        exporter._write("snapshot.json", json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False))
    return ScanOutcome(results, eligible, eligible[:10], summary, reports, folder)
