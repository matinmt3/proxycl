"""Independent scan orchestration and completed per-mode snapshots."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
import sys
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from config.loader import AppConfig

MODES = ("mtproto", "telegram", "web", "webproxy")


def network_folder(config: AppConfig) -> Path:
    config.scan.validate()
    root = Path(config.output.folder)
    return root if config.scan.network == "default" else root / "networks" / config.scan.network


def validate_request(mode: str, limit: int | None) -> None:
    if mode not in MODES:
        raise ValueError("mode must be mtproto, telegram, web or webproxy")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
        raise ValueError("Candidate count must be a positive integer or ALL")


def mode_folder(config: AppConfig, mode: str) -> Path:
    validate_request(mode, None)
    return network_folder(config) / mode


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


def _resume_pointer(config, mode):
    return Path(config.output.folder) / ".state" / f"resume-{mode}-{config.scan.network}.json"


def _checkpoint_store(config, mode, backend="json"):
    from utils.scan_state import CheckpointStore, scan_fingerprint

    store_type = CheckpointStore
    if backend == "sqlite":
        from utils.large_checkpoint import SQLiteCheckpointStore

        store_type = SQLiteCheckpointStore
    elif backend != "json":
        raise ValueError("Unknown checkpoint backend; start a fresh scan")
    return store_type(
        Path(config.output.folder) / ".state",
        mode=mode,
        network=config.scan.network,
        fingerprint=scan_fingerprint(config, mode),
        max_age_seconds=config.scan.checkpoint_max_age_minutes * 60,
    )


def _load_checkpoint(config, mode):
    path = _resume_pointer(config, mode)
    pointer = None
    if path.exists():
        with path.open("rb") as stream:
            content = stream.read(4097)
        if len(content) > 4096:
            raise ValueError("Invalid resume pointer; start a fresh scan")
        pointer = json.loads(content)
        if (
            not isinstance(pointer, dict)
            or type(pointer.get("version")) is not int
            or pointer["version"] != 1
            or pointer.get("mode") != mode
            or pointer.get("network") != config.scan.network
        ):
            raise ValueError("Resume pointer mode/network mismatch; start a fresh scan")
    store = _checkpoint_store(config, mode, pointer.get("backend", "json") if pointer else "json")
    candidates, state = store.load_for_refresh() if config.scan.refresh_stale_on_resume else store.load()
    context = state.get("context")
    if not isinstance(context, dict) or not isinstance(state.get("engine"), dict):
        raise ValueError("Checkpoint has no scan context; start a fresh scan")
    try:
        UUID(context["run_id"])
        started = datetime.fromisoformat(context["started_at"])
        if started.tzinfo is None:
            raise ValueError("Missing timezone")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Invalid checkpoint run identity or time; start a fresh scan") from exc
    if pointer is not None and pointer.get("run_id") != context.get("run_id"):
        raise ValueError("Checkpoint generation changed; start a fresh scan")
    if type(context.get("collected")) is not int or context["collected"] < len(candidates):
        raise ValueError("Checkpoint collection counts are invalid")
    validate_request(mode, context.get("requested_count"))
    if not isinstance(context.get("source_reports"), list):
        raise ValueError("Checkpoint source reports are invalid")
    return store, candidates, state


def _safe_reports(reports):
    """Keep public health fields without persisting custom feed URLs."""
    import re

    safe = []
    fields = (
        "name",
        "status",
        "count",
        "error",
        "checked",
        "qualified",
        "strict_qualified",
        "cooldown_until",
        "quality_attribution",
        "source_id",
        "publisher_id",
        "content_id",
        "fetched",
        "parsed_unique",
        "mirror_count",
    )
    for report in reports:
        row = {key: report[key] for key in fields if key in report}
        for key in ("name", "error"):
            if isinstance(row.get(key), str):
                row[key] = re.sub(r"(?:https?|file)://\S+", "[feed]", row[key])[:200]
        safe.append(row)
    return safe


def _restore_collection(collector, config, candidates, reports, mode=None):
    """Restore attribution only where a stored source name is unambiguous."""
    from collector.health import source_identity

    sources = (
        config.webproxy_sources
        if mode == "webproxy"
        else config.web_sources if mode == "web" else config.sources
    )
    enabled = [source for source in sources if source.enabled]
    names = {}
    for source in enabled:
        names.setdefault(source.name, set()).add(source_identity(source))
    collector.source_reports = reports
    collector._enabled_sources = enabled
    enabled_ids = {source_identity(source) for source in enabled}
    collector._source_by_proxy = {}
    for proxy in candidates:
        ids = [value for value in getattr(proxy, "source_memberships", []) if value in enabled_ids]
        if not ids and len(names.get(proxy.source, ())) == 1:
            ids = [next(iter(names[proxy.source]))]
        if ids:
            collector._source_by_proxy[proxy.key()] = ids
    collector._collection_completed = len(reports) == len(enabled)


def run_scan(
    config: AppConfig, mode: str = "mtproto", limit: int | None = None, export: bool = True
) -> ScanOutcome:
    from contextlib import nullcontext

    from utils.process_lock import OutputRootLock

    # Benchmarks are explicitly non-persistent and must not create output state.
    with OutputRootLock(config.output.folder) if export else nullcontext():
        return _run_scan_owned(config, mode, limit, export)


def _run_scan_owned(config: AppConfig, mode: str, limit: int | None, export: bool) -> ScanOutcome:
    from utils.user_sources import apply_user_sources

    config = apply_user_sources(config)
    validate_request(mode, limit)
    config.scan.validate()
    from utils.scan_control import ScanController

    started_clock = time.monotonic()
    requested_budget = config.scan.time_budget_seconds
    scan_controller = getattr(config, "_scan_controller", None) or ScanController()
    command = getattr(config, "_scan_command", "best10")
    favourite_proxies = getattr(config, "_favourite_proxies", None)
    config = replace(config, scan=replace(config.scan))
    config._scan_controller = scan_controller
    config._scan_command = command
    config._scan_collection_deadline = (
        started_clock + requested_budget if requested_budget is not None else None
    )
    config._scan_deadline = config._scan_collection_deadline
    if config.scan.resume and not export:
        raise ValueError("Resume needs checkpoint exports. Use best10 --resume instead of benchmark.")
    if not export:
        config = replace(
            config,
            scan=replace(
                config.scan, source_health_enabled=False, checkpoint_enabled=False, history_enabled=False
            ),
        )
        config._scan_controller = scan_controller
        config._scan_command = command
        config._scan_collection_deadline = (
            started_clock + requested_budget if requested_budget is not None else None
        )
        config._scan_deadline = config._scan_collection_deadline
    from benchmark.scorer import rank, score_all
    from exporter.exporter import Exporter, eligible_results
    from utils.interruption import ScanInterrupted
    from utils.scan_state import HistoryStore, deserialize_result, serialize_result, write_json_atomic

    if mode in ("mtproto", "telegram"):
        from collector.collector import Collector
        from tester.runner import ParallelTester

        collector, tester, exporter_type = Collector(config), ParallelTester(config), Exporter
        if mode == "telegram":
            from exporter.telegram import TelegramWebExporter

            exporter_type = TelegramWebExporter
    elif mode == "webproxy":
        from collector.telegram_webproxy import TelegramWebCollector
        from exporter.webproxy import TelegramWebProxyExporter
        from tester.runner import ParallelTester

        collector, tester, exporter_type = (
            TelegramWebCollector(config),
            ParallelTester(config),
            TelegramWebProxyExporter,
        )
    else:
        from webproxy.collector import WebCollector
        from webproxy.exporter import WebExporter
        from webproxy.tester import WebTester

        collector, tester, exporter_type = WebCollector(config), WebTester(config), WebExporter
    staged = (
        mode != "web"
        and config.testing.mtproto_profile == "telegram_session"
        and config.scan.strategy == "staged"
    )
    folder = mode_folder(config, mode)
    store, restored = None, None
    context = {"run_id": str(uuid4()), "started_at": _now()}
    interrupted = False
    profile = "web" if mode == "web" else config.testing.mtproto_profile
    refresh_stats = {}
    if config.scan.resume:
        store, selected, restored = _load_checkpoint(config, mode)
        if config.scan.refresh_stale_on_resume:
            from utils.resume_refresh import refresh_saved_evidence

            restored, refresh_stats = refresh_saved_evidence(config, selected, restored)
        context = restored["context"]
        previous_limit = context.get("requested_count")
        if limit is not None and limit != previous_limit:
            raise ValueError(
                "Checkpoint candidate count differs. Resume without --count, or start a fresh scan."
            )
        limit = previous_limit
        collected_count = context["collected"]
        _restore_collection(collector, config, selected, context["source_reports"], mode)
        print(f"\n[{mode.upper()}] Resuming saved evidence on network {config.scan.network}.", flush=True)
    else:
        print(f"\n[{mode.upper()}] Collecting source feeds | network={config.scan.network}...", flush=True)
        try:
            candidates = select_candidates(
                favourite_proxies if favourite_proxies is not None else collector.collect(), None
            )
            if favourite_proxies is not None:
                from utils.scan_state import _matches_mode

                if any(not _matches_mode(proxy, mode) for proxy in candidates):
                    raise ValueError("Favourite proxy does not match the selected mode")
        except asyncio.CancelledError as exc:
            raise KeyboardInterrupt("Collection stopped; previous saved scans are preserved") from exc
        collected_count = len(candidates)
        selected = select_candidates(candidates, limit)
        context.update(
            collected=collected_count,
            requested_count=limit,
            source_reports=_safe_reports(collector.source_reports),
        )
        if export and config.scan.checkpoint_enabled:
            store = _checkpoint_store(
                config, mode, "sqlite" if mode == "web" and len(selected) > 20_000 else "json"
            )
    if requested_budget is not None:
        config.scan.time_budget_seconds = max(0.05, requested_budget - (time.monotonic() - started_clock))
    requested = "ALL" if limit is None else str(limit)
    print(f"Collected: {collected_count} | Selected: {len(selected)} | Request: {requested}", flush=True)
    progress = {
        "stage": "baseline" if staged else "testing",
        "completed": 0,
        "total": len(selected),
        "qualified": 0,
        "remaining_seconds": None,
    }
    last_saved, saved_stage = float("-inf"), None
    progress_stage, progress_started = None, time.monotonic()
    last_printed = float("-inf")
    warnings = []

    def update_progress(update):
        nonlocal progress, progress_stage, progress_started, last_printed
        changed = update.get("stage") != progress.get("stage")
        progress = dict(update)
        if progress.get("stage") != progress_stage:
            progress_stage, progress_started = progress.get("stage"), time.monotonic()
        if progress.get("stage") == "finalists":
            progress["completed"] = progress.get("finalist_attempts_completed", 0)
            progress["total"] = progress.get("finalists_selected", 0) * config.scan.finalist_retries
        done, total = progress.get("completed", 0), progress.get("total", 0)
        progress["remaining_seconds"] = (
            ((total - done) * (time.monotonic() - progress_started) / done) if 3 <= done < total else None
        )
        now = time.monotonic()
        if changed or (sys.stdout.isatty() and now - last_printed >= 1):
            last_printed = now
            label = f"[{str(progress.get('stage', 'testing')).upper()}] {done}/{total} | passed {progress.get('qualified', 0)}"
            if progress["remaining_seconds"] is not None:
                label += f" | ETA ~{math.ceil(progress['remaining_seconds'] / 60)}m"
            if sys.stdout.isatty():
                width = max(20, shutil.get_terminal_size(fallback=(40, 24)).columns - 1)
                print("\r" + label[:width].ljust(width), end="\n" if changed else "", flush=True)
            else:
                print(label, flush=True)

    def progress_payload(active=True):
        return {
            "version": 1,
            "mode": mode,
            "network": config.scan.network,
            "run_id": context["run_id"],
            "updated_at": _now(),
            **progress,
            "active": active,
            "resumable": bool(store),
            "command": getattr(config, "_scan_command", "best10"),
        }

    def save_state(state, active=True):
        nonlocal store
        if store is not None:
            try:
                store.save(selected, {"engine": state, "context": context})
                write_json_atomic(
                    _resume_pointer(config, mode),
                    {
                        "version": 1,
                        "mode": mode,
                        "network": config.scan.network,
                        "run_id": context["run_id"],
                        "backend": "sqlite" if store.path.suffix == ".sqlite3" else "json",
                    },
                )
            except OSError as exc:
                warning = f"Checkpoint unavailable ({type(exc).__name__}); this run cannot resume."
                print(warning, flush=True)
                warnings.append(warning)
                store = None
        if export:
            try:
                write_json_atomic(folder / "progress.json", progress_payload(active))
            except OSError as exc:
                warning = f"Live progress unavailable ({type(exc).__name__})."
                if warning not in warnings:
                    print(warning, flush=True)
                    warnings.append(warning)

    async def checkpoint(state):
        nonlocal last_saved, saved_stage
        now = time.monotonic()
        interval = 5 if len(selected) > 20_000 else 1
        if now - last_saved < interval and state.get("stage") == saved_stage:
            return
        last_saved, saved_stage = now, state.get("stage")
        if staged:
            update_progress(tester.summary)
        await asyncio.to_thread(save_state, state)

    def single_state(results):
        return {
            "schema": 1,
            "stage": "testing",
            "results": [serialize_result(result) for result in results if result.samples],
        }

    if staged:
        from tester.staged import StagedTester

        tester = StagedTester(
            config,
            initial_state=restored["engine"] if restored else None,
            on_checkpoint=checkpoint,
            on_progress=update_progress,
        )
    else:
        if restored:
            engine = restored["engine"]
            if (
                engine.get("schema") != 1
                or engine.get("stage") not in ("testing", "complete")
                or not isinstance(engine.get("results"), list)
            ):
                raise ValueError("Invalid saved probe state; start a fresh scan")
            tester.initial_results = [deserialize_result(row) for row in engine["results"]]
            keys = {proxy.key() for proxy in selected}
            if len({row.proxy.key() for row in tester.initial_results}) != len(tester.initial_results) or any(
                row.proxy.key() not in keys or row.attempts > config.testing.retries
                for row in tester.initial_results
            ):
                raise ValueError("Checkpoint results do not match the selected candidates")
            if mode != "web":
                rebuilt = []
                for saved in tester.initial_results:
                    if saved.verification_profile != config.testing.mtproto_profile:
                        raise ValueError("Checkpoint verification profile differs from current config")
                    fresh = tester._new_result(saved.proxy)
                    fresh.samples = list(saved.samples)
                    rebuilt.append(fresh)
                tester.initial_results = rebuilt

        sample_checkpoint_lock = asyncio.Lock()

        async def save_finished_sample(result):
            nonlocal last_saved, saved_stage
            now = time.monotonic()
            if now - last_saved < (5 if len(selected) > 20_000 else 1):
                return
            last_saved, saved_stage = now, "testing"
            current = [
                replace(row, samples=list(row.samples))
                for row in tester._current_results
                if row is not None and row.samples
            ]
            update_progress(
                {
                    "stage": "testing",
                    "completed": sum(row.attempts >= config.testing.retries for row in current),
                    "total": len(selected),
                    "qualified": sum(row.successes > 0 for row in current),
                    "remaining_seconds": None,
                }
            )
            state = await asyncio.to_thread(single_state, current)
            await asyncio.to_thread(save_state, state)

        async def finished_sample(result):
            async with sample_checkpoint_lock:
                await save_finished_sample(result)

        tester.on_sample = finished_sample
    if export:
        save_state(
            restored["engine"]
            if restored
            else (tester.state if staged else single_state(getattr(tester, "initial_results", [])))
        )
    if selected:
        from tester.runner import worker_count

        requested_workers = config.testing.workers
        attempt_budget = config.testing.timeout_seconds + config.testing.connect_timeout_seconds
        if profile == "telegram_session":
            requested_workers = min(requested_workers, config.testing.mtproto_workers)
            attempt_budget = (
                config.testing.connect_timeout_seconds
                + 2 * config.testing.auth_timeout_seconds
                + config.testing.ping_count * config.testing.ping_timeout_seconds
                + (config.testing.ping_count - 1) * config.testing.ping_interval_seconds
                + 1  # bounded best-effort server-key cleanup
            )
            print(
                "Strict Telegram session verification: pinned server identity, encrypted pongs, "
                f"DCs {config.testing.telegram_dc_ids}, {config.testing.retries} reconnect attempts. "
                "Probe duration includes setup and hold time; Telegram RTT measures the encrypted pongs.",
                flush=True,
            )
            if config.testing.retries < max(
                config.output.mtproto_min_successes, len(config.testing.telegram_dc_ids)
            ):
                print(
                    "Configured retries cannot satisfy the strict sample/DC requirements. "
                    "Increase testing.retries; this run can save observations but no qualified links.",
                    flush=True,
                )
        elif mode != "web":
            print(
                "Transport diagnostics only. These probes do not qualify for strict top-ten links.",
                flush=True,
            )
        workers = worker_count(requested_workers, len(selected))
        estimated = math.ceil(len(selected) / workers) * config.testing.retries * attempt_budget
        print(
            f"Testing {len(selected)} candidates, {workers} workers. Approximate timeout budget: {estimated / 60:.1f} min. Ctrl+C stops and saves the best results so far.",
            flush=True,
        )
        try:
            results = tester.run(selected)
        except ScanInterrupted as exc:
            results = exc.results
            interrupted = True
        results = rank(score_all(results, config.scoring))
    else:
        results = []
    for result in results:
        result.proof_max_age_seconds = config.output.proof_max_age_minutes * 60
        result.provisional = bool(interrupted)
    output_cfg = replace(config.output, folder=str(folder))
    # Filtering does not need to create an output directory in benchmark-only runs.
    eligible = eligible_results(results, output_cfg)
    failures = Counter(s.failure_reason.value for r in results for s in r.samples if not s.success)
    reachable = sum(r.successes > 0 and r.avg_latency_ms is not None for r in results)
    strict_verified = len(eligible) if mode != "web" else 0
    summary = {
        "report_schema": 2,
        "candidate_pool_digest": hashlib.sha256(
            "\n".join(sorted(proxy.key() for proxy in selected)).encode()
        ).hexdigest(),
        "ranking_policy": config.scoring.policy,
        "scoring_settings": asdict(config.scoring),
        "ranking_experimental": config.scoring.policy == "v2-experimental",
        "proof_max_age_seconds": config.output.proof_max_age_minutes * 60,
        "expired_results": sum(not result.evidence_fresh() for result in results),
        "run_id": context["run_id"],
        "started_at": context["started_at"],
        "completed_at": _now(),
        "requested_count": limit,
        "interrupted": interrupted,
        "retry_count": config.testing.retries,
        "verification_profile": profile,
        "transport_reachable": reachable,
        "strict_verified": strict_verified,
        "collected": collected_count,
        "selected": len(selected),
        "tested": len(results),
        "fully_tested": sum(r.attempts >= config.testing.retries for r in results),
        "partial_tested": sum(0 < r.attempts < config.testing.retries for r in results),
        "skipped": len(selected) - len(results),
        "verified": strict_verified if mode != "web" else reachable,
        "eligible": len(eligible),
        "displayed": min(10, len(eligible)),
        "failure_counts": dict(sorted(failures.items())),
    }
    stage_summary = tester.summary if staged else {}
    generic_summary = getattr(tester, "summary", {}) if not staged else stage_summary
    summary.update(
        strategy="staged" if staged else "single",
        network=config.scan.network,
        resumed=bool(restored),
        stage="stopped" if interrupted else "complete",
        provisional=bool(staged and interrupted),
        interrupted_stage=stage_summary.get("stage") if interrupted else None,
        prefilter_tested=stage_summary.get("prefilter_tested", 0),
        prefilter_passed=stage_summary.get("prefilter_passed", 0),
        baseline_tested=stage_summary.get("baseline_tested", len(results)),
        finalist_selected=stage_summary.get("finalists_selected", 0),
        finalist_completed=stage_summary.get("finalists_tested", 0),
        finalist_attempts=stage_summary.get("finalist_attempts_completed", 0),
        state_warnings=warnings,
        command=getattr(config, "_scan_command", "best10"),
        time_budget_seconds=requested_budget,
        budget_exhausted=bool(generic_summary.get("budget_exhausted", False)),
        coverage_limited=bool(generic_summary.get("coverage_limited", interrupted)),
        stop_reason=scan_controller.reason
        or ("budget" if generic_summary.get("budget_exhausted") else "user" if interrupted else None),
        resume_refresh=refresh_stats,
    )
    for field in (
        "configured_workers",
        "effective_workers",
        "active_workers",
        "pressure_reason",
        "event_loop_lag_ms",
        "unexamined_candidates",
    ):
        if field in generic_summary:
            summary[field] = generic_summary[field]
    examined = (
        len(selected)
        if not interrupted
        else len(selected) - stage_summary.get("unexamined_candidates", len(selected) - len(results))
    )
    summary["examined_candidates"] = examined
    summary["coverage_percent"] = round(100 * examined / len(selected), 2) if selected else 100.0
    if staged:
        summary["prefilter_failure_counts"] = dict(
            Counter(row["reason"] for row in tester.state["prefilter"].values() if not row["passed"])
        )
    reports = _safe_reports(collector.record_quality(results))
    from collector.provenance import source_summary

    summary["sources"] = source_summary(reports)
    context["source_reports"] = reports
    update_progress(
        {
            "stage": summary["stage"],
            "completed": len(results) if interrupted else len(selected),
            "total": len(selected),
            "qualified": len(eligible),
            "remaining_seconds": None,
        }
    )
    if export:
        state = tester.state if staged else single_state(results)
        if not interrupted:
            state = {**state, "stage": "complete"}
        save_state(state, active=False)
    if export:
        import json

        exporter = exporter_type(output_cfg)
        # Preserve the last full generation while publishing useful stopped scans.
        if interrupted and not (folder / "completed_snapshot.json").exists():
            previous_path = folder / "snapshot.json"
            if previous_path.exists():
                try:
                    previous = previous_path.read_text(encoding="utf-8")
                    prior = json.loads(previous)
                    if prior.get("mode") == mode and not prior.get("summary", {}).get("interrupted", False):
                        exporter._write("completed_snapshot.json", previous)
                except (OSError, ValueError, AttributeError, UnicodeError):
                    pass
        exporter.export_all(results)
        if 10 not in output_cfg.top_sizes:
            exporter.export_top_n(eligible, 10)
        readable = exporter.export_best_n_readable(results, n=10)
        if interrupted:
            exporter._write(
                "best10_links.txt",
                "Stopped scan / partial results\n"
                f"Tested {len(results)} of {len(selected)}; skipped {summary['skipped']}.\n\n"
                + readable.read_text(encoding="utf-8"),
            )
        if config.scan.history_enabled:
            try:
                history = HistoryStore(Path(config.output.folder) / ".state", config.scan.network, mode)
                summary["history_recorded"] = min(len(results), 1000)
                summary["history_truncated"] = len(results) > 1000
                history.record(context["run_id"], results[:1000], summary)
                exporter._write(
                    "history.json",
                    json.dumps(
                        {
                            "version": 1,
                            "mode": mode,
                            "network": config.scan.network,
                            "rows": history.rows(limit=20),
                        },
                        ensure_ascii=False,
                        indent=2,
                        allow_nan=False,
                    ),
                )
            except (OSError, ValueError) as exc:
                warning = f"History unavailable ({type(exc).__name__}); current results are still exported."
                print(warning, flush=True)
                warnings.append(warning)
        # Keep completed observations even when they do not satisfy strict
        # eligibility. No session/auth keys are ever part of these models.
        observations = {
            "version": 1,
            "mode": mode,
            "summary": summary,
            "rows": [dict(r.to_dict(), samples=[asdict(sample) for sample in r.samples]) for r in results],
        }
        if staged:
            observations["prefilter_results"] = tester.state["prefilter"]
        exporter._write(
            "observations.json", json.dumps(observations, ensure_ascii=False, indent=2, allow_nan=False)
        )
        snapshot = {
            "version": 1,
            "mode": mode,
            "summary": summary,
            "source_reports": reports,
            "rows": [r.to_dict() for r in eligible],
        }
        encoded = json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False)
        exporter._write("snapshot.json", encoded)
        if not interrupted:
            exporter._write("completed_snapshot.json", encoded)
            if store is not None:
                try:
                    store.complete()
                except (OSError, ValueError) as exc:
                    print(
                        f"Could not mark checkpoint complete ({type(exc).__name__}). Current exports are saved.",
                        flush=True,
                    )
    return ScanOutcome(results, eligible, eligible[:10], summary, reports, folder)
