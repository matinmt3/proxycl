"""Local pressure and stops must preserve real samples and bound admission."""

import asyncio
import threading
import time

import pytest

from config.loader import AppConfig
from tester.runner import ParallelTester
from utils.interruption import ScanInterrupted
from utils.models import Proxy, TestSample
from utils.scan_control import AdaptiveLimiter, ScanController, ScanStopped, scan_deadline
from webproxy.models import WebProxy
from webproxy.tester import WebTester


@pytest.mark.asyncio
async def test_stop_before_admission_does_not_construct_coroutine():
    control = ScanController()
    control.request_stop()
    called = []
    with pytest.raises(ScanStopped, match="user"):
        await control.run(lambda: called.append(True))
    assert not called


@pytest.mark.asyncio
async def test_cross_thread_stop_drains_unfinished_operation_and_first_reason_wins():
    control = ScanController()
    entered, closed = asyncio.Event(), asyncio.Event()

    async def operation():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    task = asyncio.create_task(control.run(operation))
    await entered.wait()
    thread = threading.Thread(target=lambda: control.request_stop("shutdown"))
    thread.start()
    thread.join()
    control.request_stop("budget")
    with pytest.raises(ScanStopped, match="shutdown"):
        await asyncio.wait_for(task, 1)
    assert closed.is_set() and control.reason == "shutdown"


@pytest.mark.asyncio
async def test_deadline_drains_work_and_does_not_poison_following_reserved_stage():
    control = ScanController()
    closed = []

    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    with pytest.raises(ScanStopped, match="budget"):
        await control.run(operation, deadline=time.monotonic() + 0.02)
    assert closed == [True] and not control.stop_requested
    assert await control.run(lambda: asyncio.sleep(0, result=42)) == 42


@pytest.mark.asyncio
async def test_completed_operation_wins_tie_with_stop_request():
    control = ScanController()

    async def operation():
        control.request_stop()
        return "finished evidence"

    assert await control.run(operation) == "finished evidence"


def test_controller_rejects_unknown_reason():
    with pytest.raises(ValueError):
        ScanController().request_stop("network failure")


def test_original_deadline_caps_engine_budget_and_unlimited_does_not_extend_it():
    cfg = AppConfig.load()
    cfg.scan.time_budget_seconds = 20
    assert scan_deadline(cfg, started=100) == 120
    cfg._scan_deadline = 110
    assert scan_deadline(cfg, started=100) == 110
    cfg.scan.time_budget_seconds = None
    assert scan_deadline(cfg, started=100) == 110
    cfg._scan_deadline = 90
    assert scan_deadline(cfg, started=100) == 90


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "123"])
def test_invalid_private_deadline_is_rejected(value):
    cfg = AppConfig.load()
    cfg._scan_deadline = value
    with pytest.raises(ValueError, match="deadline"):
        scan_deadline(cfg)


@pytest.mark.asyncio
async def test_pressure_requires_repeated_local_stalls_and_recovers_gradually(monkeypatch):
    monkeypatch.delenv("TERMUX_VERSION", raising=False)
    monkeypatch.delenv("PREFIX", raising=False)
    limiter = AdaptiveLimiter(8)
    await limiter.observe(0.2)
    assert limiter.effective == 8
    await limiter.observe(0.2)
    assert limiter.effective == 4 and limiter.summary["pressure_reason"] == "event_loop_pressure"
    for _ in range(9):
        await limiter.observe(0)
    assert limiter.effective == 4
    await limiter.observe(0)
    assert limiter.effective == 5
    for _ in range(100):
        await limiter.observe(0)
    assert limiter.effective == 8


@pytest.mark.asyncio
async def test_disabled_pressure_never_changes_configured_admission(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "test")
    limiter = AdaptiveLimiter(16, enabled=False)
    for _ in range(10):
        await limiter.observe(2)
    assert limiter.effective == 16


def test_termux_initial_pressure_cap_respects_cpus_and_original_limit(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "test")
    monkeypatch.setattr("utils.scan_control.os.cpu_count", lambda: 2)
    assert AdaptiveLimiter(64).effective == 4
    assert AdaptiveLimiter(2).effective == 2
    assert AdaptiveLimiter(1).effective == 1


@pytest.mark.asyncio
async def test_limiter_bounds_slots_and_monitor_always_closes():
    limiter = AdaptiveLimiter(2)
    limiter.effective = 1
    active = peak = 0

    async def work():
        nonlocal active, peak
        async with limiter.slot():
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0)
            active -= 1

    async with limiter:
        await asyncio.gather(*(work() for _ in range(20)))
    assert peak == 1 and limiter.active == 0 and limiter._monitor.done()


@pytest.fixture(params=["mtproto", "web"])
def pool(request, monkeypatch):
    config = AppConfig.load()
    config.testing.workers = config.testing.mtproto_workers = 1
    config.testing.mtproto_profile = "transport"
    config.scan.time_budget_seconds = 0.2
    if request.param == "mtproto":
        import tester.runner as module

        proxies = [Proxy(f"proxy{index}.example", 443, "a" * 32) for index in range(3)]
        constructor = ParallelTester
    else:
        import webproxy.tester as module

        proxies = [WebProxy(f"proxy{index}.example", 8080) for index in range(3)]
        constructor = WebTester
    return config, module, constructor, proxies


@pytest.mark.asyncio
async def test_timeboxed_single_pools_keep_only_finished_attempts_and_resume(pool, monkeypatch):
    config, module, constructor, proxies = pool
    calls = []
    closed = []

    async def first_probe(proxy, *args, **kwargs):
        calls.append(proxy.key())
        if len(calls) == 1:
            return TestSample(True, total_latency_ms=10)
        try:
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    monkeypatch.setattr(module, "test_proxy_once", first_probe)
    tester = constructor(config)
    with pytest.raises(ScanStopped, match="budget"):
        await tester.run_async(proxies)
    assert len(tester.partial_results) == 1 and tester.partial_results[0].attempts == 1
    assert tester.partial_results[0].successes == 1 and closed == [True]
    assert tester.summary["budget_exhausted"]

    async def resumed_probe(*args, **kwargs):
        return TestSample(True, total_latency_ms=20)

    monkeypatch.setattr(module, "test_proxy_once", resumed_probe)
    config.scan.time_budget_seconds = None
    resumed = constructor(config)
    resumed.initial_results = tester.partial_results
    results = await resumed.run_async(proxies)
    assert [result.attempts for result in results] == [3, 3, 3]
    assert results[0].samples[0].total_latency_ms == 10


def test_synchronous_pool_translates_dashboard_stop_without_new_probe(monkeypatch):
    config = AppConfig.load()
    config.testing.mtproto_profile = "transport"
    control = ScanController()
    control.request_stop()
    tester = ParallelTester(config, controller=control)
    with pytest.raises(ScanInterrupted) as stopped:
        tester.run([Proxy("proxy.example", 443, "a" * 32)])
    assert stopped.value.results == []
