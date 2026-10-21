"""Staged scanning must earn recommendations and resume only finished work."""

import asyncio
import copy
import json
from collections import Counter

import pytest

from config.loader import AppConfig
from tester.staged import StagedTester
from utils.interruption import ScanInterrupted
from utils.models import FailureReason, Proxy, TestSample


def candidates(count=4):
    return [Proxy(f"proxy{index}.example", 443, "a" * 32) for index in range(count)]


def config():
    value = AppConfig.load()
    value.testing.workers = value.testing.mtproto_workers = 2
    value.scan.finalists = 2
    value.scan.finalist_workers = 1
    value.scan.finalist_ping_interval_seconds = 0.1
    value.scan.finalist_reconnect_delay_seconds = 0
    return value


def passing_sample(dc=2, pings=3, latency=100):
    return TestSample(
        True,
        total_latency_ms=latency + 1000,
        verification_level="telegram_session",
        dc_id=dc,
        ping_rtts_ms=[float(latency)] * pings,
    )


def install_probes(monkeypatch, *, strict=None, prefilter=None):
    async def default_prefilter(proxy, **kwargs):
        return TestSample(True, total_latency_ms=1)

    async def default_strict(proxy, **kwargs):
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"], int(proxy.server[5]) + 50)

    monkeypatch.setattr("tester.staged.test_proxy_once", prefilter or default_prefilter)
    monkeypatch.setattr("tester.staged.test_proxy_strict", strict or default_strict)


@pytest.mark.asyncio
async def test_normal_completion_requires_finalist_evidence_for_every_recommendation(monkeypatch):
    install_probes(monkeypatch)
    engine = StagedTester(config())
    results = await engine.run_async(candidates())
    assert [result.attempts for result in results] == [8, 8, 3, 3]
    assert [result.strict_eligible for result in results] == [True, True, False, False]
    assert all(result.quality_policy.min_successes == 7 for result in results)
    assert [len(sample.ping_rtts_ms) for sample in results[0].samples] == [3, 3, 3, 10, 10, 10, 10, 10]
    assert [sample.dc_id for sample in results[0].samples] == [2, 4, 2, 4, 2, 4, 2, 4]
    assert engine.summary["stage"] == "complete" and not engine.summary["provisional"]
    assert engine.summary["finalists_selected"] == 2
    assert engine.summary["strict_attempts_completed"] == 22
    assert engine.summary["qualified"] == 2


@pytest.mark.asyncio
async def test_prefilter_failures_do_not_inflate_strict_attempt_denominators(monkeypatch):
    async def prefilter(proxy, **kwargs):
        return TestSample(
            proxy.server != "proxy1.example",
            failure_reason=(
                FailureReason.TCP_TIMEOUT if proxy.server == "proxy1.example" else FailureReason.NONE
            ),
        )

    install_probes(monkeypatch, prefilter=prefilter)
    engine = StagedTester(config())
    results = await engine.run_async(candidates(3))
    assert len(results) == 2 and all(result.attempts == 8 for result in results)
    assert engine.summary["prefilter_failed"] == 1
    assert engine.state["prefilter"][candidates(3)[1].key()] == {"passed": False, "reason": "tcp_timeout"}
    assert engine.summary["strict_attempts_completed"] == 16


@pytest.mark.asyncio
async def test_failed_finalists_are_not_replaced_by_short_baseline_results(monkeypatch):
    calls = Counter()

    async def strict(proxy, **kwargs):
        calls[proxy.key()] += 1
        if calls[proxy.key()] > 3:
            return TestSample(False, failure_reason=FailureReason.HANDSHAKE_TIMEOUT)
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"], int(proxy.server[5]) + 50)

    install_probes(monkeypatch, strict=strict)
    engine = StagedTester(config())
    results = await engine.run_async(candidates())
    assert not any(result.strict_eligible for result in results)
    assert [result.attempts for result in results] == [8, 8, 3, 3]
    assert results[0].success_rate == 3 / 8
    assert engine.summary["baseline_qualified"] == 4
    assert engine.summary["qualified"] == 0


@pytest.mark.asyncio
async def test_cancelled_finalist_run_keeps_finished_baseline_and_no_invented_failure(monkeypatch):
    entered = asyncio.Event()
    calls = Counter()

    async def strict(proxy, **kwargs):
        calls[proxy.key()] += 1
        if calls[proxy.key()] == 5:
            entered.set()
            await asyncio.Event().wait()
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"], int(proxy.server[5]) + 50)

    install_probes(monkeypatch, strict=strict)
    engine = StagedTester(config())
    task = asyncio.create_task(engine.run_async(candidates(2)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [result.attempts for result in engine.partial_results] == [4, 3]
    assert all(result.strict_eligible for result in engine.partial_results)
    assert engine.summary["provisional"] and engine.summary["stage"] == "finalists"
    assert engine.state["finalist_attempts"][candidates(2)[0].key()] == 1
    assert engine.summary["strict_attempts_completed"] == 7
    assert all(sample.success for result in engine.partial_results for sample in result.samples)


@pytest.mark.asyncio
async def test_resume_finalists_reuses_finished_sessions_and_continues_dc_rotation(monkeypatch):
    installed = []
    entered = asyncio.Event()

    async def first_run(proxy, **kwargs):
        installed.append(kwargs["dc_id"])
        if len(installed) == 5:
            entered.set()
            await asyncio.Event().wait()
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=first_run)
    engine = StagedTester(config())
    task = asyncio.create_task(engine.run_async(candidates(1)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = json.loads(json.dumps(engine.state))
    resumed_calls = []

    async def resumed_run(proxy, **kwargs):
        resumed_calls.append(kwargs)
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    async def forbidden_prefilter(*args, **kwargs):
        raise AssertionError("finished prefilter must not run again")

    install_probes(monkeypatch, strict=resumed_run, prefilter=forbidden_prefilter)
    resumed = StagedTester(config(), initial_state=saved)
    results = await resumed.run_async(candidates(1))
    assert len(resumed_calls) == 4
    assert [call["dc_id"] for call in resumed_calls] == [2, 4, 2, 4]
    assert all(call["ping_count"] == 10 for call in resumed_calls)
    assert results[0].attempts == 8 and results[0].strict_eligible
    assert saved == json.loads(json.dumps(engine.state)), "resume must not mutate caller checkpoint"


@pytest.mark.asyncio
async def test_checkpoint_after_every_finished_sample_is_awaited_and_immutable(monkeypatch):
    install_probes(monkeypatch)
    snapshots = []
    progress = []

    async def checkpoint(state):
        await asyncio.sleep(0)
        snapshots.append((state, copy.deepcopy(state)))

    async def on_progress(state):
        await asyncio.sleep(0)
        progress.append(state)

    engine = StagedTester(config(), on_checkpoint=checkpoint, on_progress=on_progress)
    await engine.run_async(candidates(2))
    assert len(snapshots) >= 2 + 16
    assert all(snapshot == preserved for snapshot, preserved in snapshots)
    assert progress[0]["stage"] == "prefilter" and progress[-1]["stage"] == "complete"
    assert all({"stage", "completed", "total", "qualified"} <= row.keys() for row in progress)
    assert json.loads(json.dumps(engine.state))["stage"] == "complete"


@pytest.mark.asyncio
async def test_cancel_during_checkpoint_wait_retains_all_finished_worker_samples(monkeypatch):
    install_probes(monkeypatch)
    blocking = asyncio.Event()
    callbacks = []

    async def checkpoint(state):
        callbacks.append(state)
        if state["stage"] == "baseline" and state["results"]:
            blocking.set()
            await asyncio.Event().wait()

    engine = StagedTester(config(), on_checkpoint=checkpoint)
    task = asyncio.create_task(engine.run_async(candidates(2)))
    await blocking.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(engine.partial_results) == 2
    assert [result.attempts for result in engine.partial_results] == [1, 1]
    assert len(engine.state["results"]) == 2
    assert sum(len(row["samples"]) for row in engine.state["results"]) == 2
    resumed = StagedTester(config(), initial_state=engine.state)
    assert all(result.strict_eligible for result in await resumed.run_async(candidates(2)))


@pytest.mark.asyncio
async def test_concurrency_is_bounded_in_each_stage_and_finalist_selection_is_frozen(monkeypatch):
    cfg = config()
    cfg.scan.finalists = 3
    cfg.scan.finalist_workers = 1
    active = Counter()
    peak = Counter()
    completed = Counter()

    async def prefilter(proxy, **kwargs):
        active["prefilter"] += 1
        peak["prefilter"] = max(active["prefilter"], peak["prefilter"])
        await asyncio.sleep(0)
        active["prefilter"] -= 1
        return TestSample(True)

    async def strict(proxy, **kwargs):
        stage = "baseline" if kwargs["ping_count"] == 3 else "finalists"
        active[stage] += 1
        peak[stage] = max(active[stage], peak[stage])
        await asyncio.sleep(0)
        active[stage] -= 1
        completed[(stage, proxy.key())] += 1
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"], int(proxy.server[5]) + 50)

    install_probes(monkeypatch, strict=strict, prefilter=prefilter)
    engine = StagedTester(cfg)
    await engine.run_async(candidates(8))
    assert peak == {"prefilter": 2, "baseline": 2, "finalists": 1}
    assert sum(count for (stage, _), count in completed.items() if stage == "finalists") == 15
    assert engine.state["finalist_keys"] == [proxy.key() for proxy in candidates(3)]


@pytest.mark.asyncio
async def test_prefilter_optout_still_tests_baseline_and_never_creates_transport_evidence(monkeypatch):
    cfg = config()
    cfg.scan.prefilter = False

    async def forbidden_prefilter(*args, **kwargs):
        raise AssertionError("prefilter disabled")

    install_probes(monkeypatch, prefilter=forbidden_prefilter)
    engine = StagedTester(cfg)
    results = await engine.run_async(candidates(1))
    assert results[0].attempts == 8 and results[0].strict_eligible
    assert engine.state["prefilter"] == {}


@pytest.mark.asyncio
async def test_no_baseline_qualified_does_not_run_finalist_probe(monkeypatch):
    async def strict(proxy, **kwargs):
        assert kwargs["ping_count"] == 3
        return TestSample(False, failure_reason=FailureReason.TCP_REFUSED)

    install_probes(monkeypatch, strict=strict)
    engine = StagedTester(config())
    results = await engine.run_async(candidates())
    assert len(results) == 4 and not any(result.strict_eligible for result in results)
    assert engine.summary["finalists_selected"] == 0 and engine.summary["stage"] == "complete"


@pytest.mark.asyncio
async def test_cancelled_baseline_resume_does_not_repeat_finished_attempts(monkeypatch):
    cfg = config()
    cfg.testing.mtproto_workers = 1
    entered = asyncio.Event()
    probes = []

    async def strict(proxy, **kwargs):
        probes.append((proxy.key(), kwargs["dc_id"]))
        if len(probes) == 2:
            entered.set()
            await asyncio.Event().wait()
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=strict)
    engine = StagedTester(cfg)
    task = asyncio.create_task(engine.run_async(candidates(1)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.partial_results[0].attempts == 1
    resumed_calls = []

    async def resumed_probe(proxy, **kwargs):
        resumed_calls.append(kwargs)
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=resumed_probe)
    resumed = StagedTester(cfg, initial_state=engine.state)
    results = await resumed.run_async(candidates(1))
    assert [row["dc_id"] for row in resumed_calls] == [4, 2, 4, 2, 4, 2, 4]
    assert results[0].attempts == 8


@pytest.mark.asyncio
async def test_cancelled_prefilter_resumes_only_unfinished_candidates(monkeypatch):
    cfg = config()
    cfg.testing.mtproto_workers = 1
    entered = asyncio.Event()
    prefilter_calls = []

    async def prefilter(proxy, **kwargs):
        prefilter_calls.append(proxy.key())
        if len(prefilter_calls) == 2:
            entered.set()
            await asyncio.Event().wait()
        return TestSample(True)

    install_probes(monkeypatch, prefilter=prefilter)
    engine = StagedTester(cfg)
    task = asyncio.create_task(engine.run_async(candidates(3)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(engine.state["prefilter"]) == 1 and not engine.partial_results
    resumed_calls = []

    async def resumed_prefilter(proxy, **kwargs):
        resumed_calls.append(proxy.key())
        return TestSample(True)

    install_probes(monkeypatch, prefilter=resumed_prefilter)
    resumed = StagedTester(cfg, initial_state=engine.state)
    await resumed.run_async(candidates(3))
    assert resumed_calls == [proxy.key() for proxy in candidates(3)[1:]]


@pytest.mark.asyncio
async def test_cancel_before_first_strict_sample_does_not_save_unresumable_empty_result(monkeypatch):
    entered = asyncio.Event()

    async def blocked_strict(proxy, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    install_probes(monkeypatch, strict=blocked_strict)
    engine = StagedTester(config())
    task = asyncio.create_task(engine.run_async(candidates(1)))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert engine.state["results"] == [] and not engine.partial_results
    install_probes(monkeypatch)
    resumed = StagedTester(config(), initial_state=engine.state)
    result = (await resumed.run_async(candidates(1)))[0]
    assert result.attempts == 8 and result.strict_eligible


@pytest.mark.asyncio
async def test_empty_candidates_complete_without_network(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("empty candidates must not call network")

    install_probes(monkeypatch, strict=forbidden, prefilter=forbidden)
    engine = StagedTester(config())
    assert await engine.run_async([]) == []
    assert engine.summary["stage"] == "complete" and engine.summary["qualified"] == 0


@pytest.mark.asyncio
async def test_large_baseline_budget_cannot_bypass_finalist_selection(monkeypatch):
    cfg = config()
    cfg.testing.retries = 8
    cfg.scan.finalists = 1
    cfg.scan.finalist_retries = 1
    install_probes(monkeypatch)
    results = await StagedTester(cfg).run_async(candidates(2))
    assert [result.strict_eligible for result in results] == [True, False]
    assert results[1].quality_policy.min_successes > results[1].attempts


@pytest.mark.asyncio
async def test_finalist_ping_count_never_weakens_configured_baseline_proof(monkeypatch):
    cfg = config()
    cfg.testing.ping_count = 10
    cfg.scan.finalist_ping_count = 3
    install_probes(monkeypatch)
    result = (await StagedTester(cfg).run_async(candidates(1)))[0]
    assert result.strict_eligible
    assert all(len(sample.ping_rtts_ms) == 10 for sample in result.samples)


@pytest.mark.parametrize(
    "state",
    [
        {"schema": 2, "stage": "prefilter"},
        {"schema": 1, "stage": "unknown"},
        {
            "schema": 1,
            "stage": "baseline",
            "prefilter": {},
            "results": [],
            "finalist_keys": [],
            "finalist_attempts": {},
        },
    ],
)
@pytest.mark.asyncio
async def test_invalid_checkpoint_is_rejected_before_network(monkeypatch, state):
    async def forbidden(*args, **kwargs):
        raise AssertionError("malformed checkpoint reached network")

    install_probes(monkeypatch, strict=forbidden, prefilter=forbidden)
    with pytest.raises(ValueError):
        await StagedTester(config(), initial_state=state).run_async(candidates(1))


@pytest.mark.parametrize(
    "tamper",
    [
        lambda state: state.update(stage="prefilter"),
        lambda state: state.update(finalist_keys=[]),
        lambda state: state["finalist_attempts"].update({next(iter(state["finalist_attempts"])): 4}),
        lambda state: state["results"][0]["samples"].pop(),
        lambda state: state["results"].append(copy.deepcopy(state["results"][0])),
        lambda state: state["prefilter"].pop(next(iter(state["prefilter"]))),
        lambda state: state["prefilter"].update(
            {"unselected:443:secret": {"passed": True, "reason": "none"}}
        ),
    ],
)
@pytest.mark.asyncio
async def test_resume_rejects_inconsistent_finished_stage_counts_before_network(monkeypatch, tamper):
    install_probes(monkeypatch)
    engine = StagedTester(config())
    await engine.run_async(candidates(1))
    state = copy.deepcopy(engine.state)
    tamper(state)

    async def forbidden(*args, **kwargs):
        raise AssertionError("inconsistent checkpoint reached network")

    install_probes(monkeypatch, strict=forbidden, prefilter=forbidden)
    with pytest.raises(ValueError):
        await StagedTester(config(), initial_state=state).run_async(candidates(1))


@pytest.mark.asyncio
async def test_complete_checkpoint_reconstructs_policy_ignores_saved_score_and_no_retests(monkeypatch):
    install_probes(monkeypatch)
    engine = StagedTester(config())
    await engine.run_async(candidates(3))
    state = copy.deepcopy(engine.state)
    for row in state["results"]:
        row["score"] = 1
        row["strict_eligible"] = True
        row["quality_policy"]["min_successes"] = 3

    async def forbidden(*args, **kwargs):
        raise AssertionError("already complete staged scan must not run extra sessions")

    install_probes(monkeypatch, strict=forbidden, prefilter=forbidden)
    resumed = StagedTester(config(), initial_state=state)
    results = await resumed.run_async(candidates(3))
    assert [result.strict_eligible for result in results] == [True, True, False]
    assert all(result.score == 0 for result in results)
    assert all(result.quality_policy.min_successes == 7 for result in results)
    assert resumed.summary["strict_attempts_completed"] == 19


def test_sync_run_translates_cancelled_error_with_finished_results(monkeypatch):
    async def prefilter(proxy, **kwargs):
        return TestSample(True)

    async def strict(proxy, **kwargs):
        if kwargs["dc_id"] == 4:
            raise asyncio.CancelledError
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=strict, prefilter=prefilter)
    with pytest.raises(ScanInterrupted) as stopped:
        StagedTester(config()).run(candidates(1))
    assert stopped.value.results[0].attempts == 1
