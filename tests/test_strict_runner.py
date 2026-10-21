import asyncio

import pytest

from config.loader import AppConfig
from tester.runner import ParallelTester
from utils.models import Proxy, TestSample


def proxies(count):
    return [Proxy(f"proxy-{n}.example", 443, "01" * 16) for n in range(count)]


def sample(dc):
    return TestSample(
        True,
        total_latency_ms=1250,
        verification_level="telegram_session",
        dc_id=dc,
        ping_rtts_ms=[10, 12, 14],
    )


@pytest.mark.asyncio
async def test_default_runner_uses_authenticated_sessions_rotating_dcs_and_caps_concurrency(monkeypatch):
    import tester.mtproto as protocol

    config = AppConfig.load()
    config.testing.mtproto_workers = 2
    calls = {}
    active = peak = 0

    async def strict(proxy, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            calls.setdefault(proxy.server, []).append(kwargs)
            await asyncio.sleep(0)
            return sample(kwargs["dc_id"])
        finally:
            active -= 1

    async def wrong_probe(*args, **kwargs):
        pytest.fail("Default runner performed only an unauthenticated transport probe")

    monkeypatch.setattr(protocol, "test_proxy_strict", strict, raising=False)
    monkeypatch.setattr("tester.runner.test_proxy_once", wrong_probe)
    rows = await ParallelTester(config).run_async(proxies(8))
    assert peak == 2
    assert all(row.verification_profile == "telegram_session" and row.strict_eligible for row in rows)
    for options in calls.values():
        assert options == [
            {
                "connect_timeout": 3.0,
                "handshake_timeout": 12.0,
                "dc_id": dc,
                "ping_count": 3,
                "ping_interval": 0.5,
                "ping_timeout": 3.0,
            }
            for dc in (2, 4, 2)
        ]


@pytest.mark.asyncio
async def test_interruption_keeps_strict_profile_policy_and_only_returned_samples(monkeypatch):
    import tester.mtproto as protocol

    config = AppConfig.load()
    config.testing.workers = 1
    config.output.mtproto_max_ping_p95_ms = 777
    waiting = asyncio.Event()
    counts = {}

    async def strict(proxy, **kwargs):
        counts[proxy.server] = counts.get(proxy.server, 0) + 1
        if proxy.server == "proxy-1.example" and counts[proxy.server] == 2:
            waiting.set()
            await asyncio.Event().wait()
        return sample(kwargs["dc_id"])

    async def wrong_probe(*args, **kwargs):
        pytest.fail("Strict pool called transport diagnostic")

    monkeypatch.setattr(protocol, "test_proxy_strict", strict, raising=False)
    monkeypatch.setattr("tester.runner.test_proxy_once", wrong_probe)
    pool = ParallelTester(config)
    task = asyncio.create_task(pool.run_async(proxies(3)))
    try:
        await asyncio.wait_for(waiting.wait(), 2)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [(row.attempts, row.strict_eligible) for row in pool.partial_results] == [(3, True), (1, False)]
    assert all(
        row.verification_profile == "telegram_session" and row.quality_policy.max_p95_ms == 777
        for row in pool.partial_results
    )
    assert all(row.to_dict()["strict_policy"]["max_p95_ms"] == 777 for row in pool.partial_results)


@pytest.mark.asyncio
async def test_explicit_transport_mode_keeps_diagnostic_label_and_never_qualifies(monkeypatch):
    config = AppConfig.load()
    config.testing.mtproto_profile = "transport"

    async def transport(proxy, **kwargs):
        return TestSample(True, total_latency_ms=1)

    monkeypatch.setattr("tester.runner.test_proxy_once", transport)
    rows = await ParallelTester(config).run_async(proxies(1))
    assert rows[0].verification_profile == "transport" and rows[0].attempts == 3
    assert rows[0].strict_eligible is False
