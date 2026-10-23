"""A reserved finalist window never turns untested candidates into failures."""

import asyncio
import copy
from collections import Counter

import pytest

from tester.staged import StagedTester
from tests.test_staged import candidates, config, install_probes, passing_sample
from utils.models import TestSample
from utils.scan_control import ScanController, ScanStopped


@pytest.mark.asyncio
async def test_budget_reserves_finalist_time_then_resume_reopens_pending_baseline(monkeypatch):
    cfg = config()
    cfg.testing.workers = cfg.testing.mtproto_workers = 1
    cfg.scan.finalists = 1
    cfg.scan.time_budget_seconds = 0.15
    cfg.scan.finalist_reserve_fraction = 0.4
    calls = Counter()

    async def probe(proxy, **kwargs):
        calls[proxy.key()] += 1
        if proxy.server != "proxy0.example" and kwargs["ping_count"] == 3:
            await asyncio.Event().wait()
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=probe)
    engine = StagedTester(cfg)
    with pytest.raises(ScanStopped, match="budget"):
        await engine.run_async(candidates(3))
    assert [result.attempts for result in engine.partial_results] == [8]
    assert engine.partial_results[0].strict_eligible
    assert engine.summary["provisional"] and engine.summary["budget_exhausted"]
    assert engine.summary["coverage_limited"] and engine.summary["unexamined_candidates"] == 2
    assert engine.state["budget_round_finished"]
    assert engine.summary["stage"] == "finalists"

    resumed_calls = Counter()

    async def resumed_probe(proxy, **kwargs):
        resumed_calls[proxy.key()] += 1
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=resumed_probe)
    cfg.scan.time_budget_seconds = None
    resumed = StagedTester(cfg, initial_state=engine.state)
    results = await resumed.run_async(candidates(3))
    assert resumed_calls[candidates()[0].key()] == 0
    assert [result.attempts for result in results] == [8, 8, 3]
    assert [result.strict_eligible for result in results] == [True, True, False]
    assert resumed.summary["stage"] == "complete" and not resumed.summary["coverage_limited"]


@pytest.mark.asyncio
async def test_prefilter_budget_keeps_unexamined_pool_for_later_resume(monkeypatch):
    cfg = config()
    cfg.testing.workers = cfg.testing.mtproto_workers = 1
    cfg.scan.finalists = 1
    cfg.scan.time_budget_seconds = 0.15

    async def prefilter(proxy, **kwargs):
        if proxy.server != "proxy0.example":
            await asyncio.Event().wait()
        return TestSample(True)

    install_probes(monkeypatch, prefilter=prefilter)
    engine = StagedTester(cfg)
    with pytest.raises(ScanStopped, match="budget"):
        await engine.run_async(candidates(3))
    assert len(engine.state["prefilter"]) == 1
    assert [result.attempts for result in engine.partial_results] == [8]
    assert engine.summary["prefilter_failed"] == 0

    calls = []

    async def resumed_prefilter(proxy, **kwargs):
        calls.append(proxy.key())
        return TestSample(True)

    install_probes(monkeypatch, prefilter=resumed_prefilter)
    cfg.scan.time_budget_seconds = None
    resumed = StagedTester(cfg, initial_state=engine.state)
    results = await resumed.run_async(candidates(3))
    assert calls == [proxy.key() for proxy in candidates(3)[1:]]
    assert [result.attempts for result in results] == [8, 8, 3]


@pytest.mark.asyncio
async def test_budget_cancelled_mid_finalist_continues_frozen_attempts_without_extra(monkeypatch):
    cfg = config()
    cfg.testing.workers = cfg.testing.mtproto_workers = 1
    cfg.scan.time_budget_seconds = 0.08
    calls = 0

    async def probe(proxy, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 5:
            await asyncio.Event().wait()
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=probe)
    engine = StagedTester(cfg)
    with pytest.raises(ScanStopped, match="budget"):
        await engine.run_async(candidates(1))
    assert engine.partial_results[0].attempts == 4
    assert not engine.state["budget_round_finished"]
    assert not engine.summary["coverage_limited"]
    cfg.scan.time_budget_seconds = None
    install_probes(monkeypatch)
    resumed = StagedTester(cfg, initial_state=engine.state)
    result = (await resumed.run_async(candidates(1)))[0]
    assert result.attempts == 8 and result.strict_eligible


@pytest.mark.asyncio
async def test_refresh_contract_keeps_fresh_partial_finalists_and_retests_missing_screens(monkeypatch):
    cfg = config()
    install_probes(monkeypatch)
    completed = StagedTester(cfg)
    await completed.run_async(candidates(3))
    state = copy.deepcopy(completed.state)
    key = candidates()[0].key()
    state.update(
        stage="prefilter",
        prefilter={key: {"passed": True, "reason": "none"}},
        results=state["results"][:1],
        finalist_keys=[],
        finalist_attempts={},
        completed_finalists={key: 1},
        coverage_limited=True,
        budget_round_finished=False,
    )
    state["results"][0]["samples"] = state["results"][0]["samples"][:4]
    calls = Counter()

    async def probe(proxy, **kwargs):
        calls[proxy.key()] += 1
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=probe)
    resumed = StagedTester(cfg, initial_state=state)
    results = await resumed.run_async(candidates(3))
    assert calls[key] == 4 and results[0].attempts == 8
    assert [result.attempts for result in results] == [8, 8, 3]
    assert all(result.quality_policy.min_successes == 7 for result in results)


@pytest.mark.asyncio
async def test_dashboard_stop_is_distinct_from_budget_and_admits_no_next_attempt(monkeypatch):
    cfg = config()
    cfg.testing.workers = cfg.testing.mtproto_workers = 1
    control = ScanController()
    calls = []

    async def probe(proxy, **kwargs):
        calls.append(proxy.key())
        control.request_stop()
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=probe)
    engine = StagedTester(cfg, controller=control)
    with pytest.raises(asyncio.CancelledError):
        await engine.run_async(candidates(2))
    assert calls == [candidates()[0].key()]
    assert [result.attempts for result in engine.partial_results] == [1]
    assert not engine.summary["budget_exhausted"]


@pytest.mark.parametrize(
    "change",
    [
        {"coverage_limited": "true"},
        {"coverage_limited": True},
        {"budget_round_finished": True},
        {"completed_finalists": {"unknown": 5}},
    ],
)
@pytest.mark.asyncio
async def test_tampered_budget_completion_cannot_claim_unearned_complete(monkeypatch, change):
    install_probes(monkeypatch)
    engine = StagedTester(config())
    await engine.run_async(candidates(1))
    saved = copy.deepcopy(engine.state)
    saved.update(change)
    with pytest.raises(ValueError):
        await StagedTester(config(), initial_state=saved).run_async(candidates(1))
