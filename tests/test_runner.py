import asyncio

import pytest

from config.loader import AppConfig
from tester.runner import ParallelTester, worker_count
from utils.models import Proxy, TestSample


class Progress:
    def __init__(self):
        self.updated = 0
        self.closed = False

    def update(self, value):
        self.updated += value

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_worker_pool_bounds_tasks_connections_and_preserves_input_order(monkeypatch):
    active = peak = 0
    created = []
    original_create = asyncio.create_task

    def record_task(coro, **kwargs):
        task = original_create(coro, **kwargs)
        created.append(task)
        return task

    async def probe(proxy, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return TestSample(success=True, total_latency_ms=1)

    bar = Progress()
    monkeypatch.setattr("tester.runner.test_proxy_once", probe)
    monkeypatch.setattr("tester.runner.progress_bar", lambda **kwargs: bar)
    monkeypatch.setattr("tester.runner.asyncio.create_task", record_task)
    config = AppConfig.load()
    config.testing.workers = 3
    config.testing.retries = 2
    proxies = [Proxy(f"proxy{i}.example", 443, "00112233445566778899aabbccddeeff") for i in range(100)]
    results = await ParallelTester(config).run_async(proxies)
    assert len(created) == 3 and peak <= 3
    assert [result.proxy for result in results] == proxies
    assert all(result.attempts == 2 for result in results)
    assert bar.closed and bar.updated == 100


@pytest.mark.asyncio
async def test_worker_pool_cancellation_drains_workers_and_closes_progress(monkeypatch):
    entered = asyncio.Event()
    active = 0

    async def probe(proxy, **kwargs):
        nonlocal active
        active += 1
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    bar = Progress()
    monkeypatch.setattr("tester.runner.test_proxy_once", probe)
    monkeypatch.setattr("tester.runner.progress_bar", lambda **kwargs: bar)
    config = AppConfig.load()
    config.testing.workers = 2
    task = asyncio.create_task(ParallelTester(config).run_async([Proxy("example.com", 443, "a" * 32)] * 100))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert active == 0 and bar.closed


def test_termux_limits_workers_without_exceeding_candidate_count(monkeypatch):
    monkeypatch.setenv("TERMUX_VERSION", "test")
    assert worker_count(500, 1000) <= 64
    assert worker_count(500, 2) == 2
