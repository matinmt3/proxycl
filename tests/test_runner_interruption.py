"""Interrupting a pool preserves observed samples without inventing attempts."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from utils.models import FailureReason, Proxy, TestSample
from webproxy.models import WebProxy


class Progress:
    def __init__(self):
        self.completed = 0
        self.closed = False

    def update(self, value):
        self.completed += value

    def close(self):
        self.closed = True


@pytest.fixture(params=["mtproto", "web"])
def pool(request, monkeypatch):
    config = SimpleNamespace(
        testing=SimpleNamespace(workers=2, retries=2, connect_timeout_seconds=1, timeout_seconds=2)
    )
    bar = Progress()
    if request.param == "mtproto":
        import tester.runner as module

        instance = module.ParallelTester(config)
        monkeypatch.setattr(module, "progress_bar", lambda **kwargs: bar)
        proxies = [
            Proxy(name, 443, "00112233445566778899aabbccddeeff")
            for name in ("completed.example", "partial.example", "untested.example", "never-started.example")
        ]
    else:
        import webproxy.tester as module

        instance = module.WebTester(config)
        monkeypatch.setattr("utils.logger.progress_bar", lambda **kwargs: bar)
        proxies = [
            WebProxy(name, 8080, "http")
            for name in ("completed.example", "partial.example", "untested.example", "never-started.example")
        ]
    return instance, module, proxies, bar


@pytest.mark.asyncio
async def test_async_cancellation_retains_completed_and_partial_retry_samples_only(pool, monkeypatch):
    instance, module, proxies, bar = pool
    partial_waiting = asyncio.Event()
    unsampled_waiting = asyncio.Event()
    calls = {}
    active = 0

    async def network_probe(proxy, *args, **kwargs):
        nonlocal active
        calls[proxy.server] = calls.get(proxy.server, 0) + 1
        attempt = calls[proxy.server]
        if proxy.server == "completed.example":
            await asyncio.sleep(0)
            if attempt == 1:
                return TestSample(False, failure_reason=FailureReason.TCP_REFUSED)
            return TestSample(True, total_latency_ms=30)
        if proxy.server == "partial.example" and attempt == 1:
            return TestSample(True, total_latency_ms=10)
        active += 1
        if proxy.server == "partial.example":
            partial_waiting.set()
        else:
            unsampled_waiting.set()
        try:
            await asyncio.Event().wait()
        finally:
            active -= 1

    monkeypatch.setattr(module, "test_proxy_once", network_probe)
    before = asyncio.all_tasks()
    task = asyncio.create_task(instance.run_async(proxies))
    await asyncio.wait_for(asyncio.gather(partial_waiting.wait(), unsampled_waiting.wait()), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    rows = getattr(instance, "partial_results", [])
    assert [(row.proxy.server, row.attempts, row.successes) for row in rows] == [
        ("completed.example", 2, 1),
        ("partial.example", 1, 1),
    ]
    assert rows[0].samples[0].failure_reason == FailureReason.TCP_REFUSED
    assert rows[1].samples[0].total_latency_ms == 10
    assert active == 0 and bar.closed and bar.completed == 1
    assert calls == {"completed.example": 2, "partial.example": 2, "untested.example": 1}
    assert asyncio.all_tasks() <= before
    assert await instance.run_async([]) == []
    assert instance.partial_results == []


@pytest.mark.parametrize("mode", ["mtproto", "web"])
def test_native_sigint_in_subprocess_returns_observed_samples_and_closes_sockets(mode):
    script = Path(__file__).parent / "fixtures" / "interruption_child.py"
    result = subprocess.run(
        [sys.executable, "-u", str(script), mode],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(
        next(
            line.removeprefix("REPORT:") for line in result.stdout.splitlines() if line.startswith("REPORT:")
        )
    )
    assert report["signal"] == "SIGINT"
    assert report["exception"] == "ScanInterrupted"
    assert report["rows"] == [
        {"server": "completed.example", "attempts": 2, "successes": 1},
        {"server": "partial.example", "attempts": 1, "successes": 1},
    ]
    assert report["opened"] == report["closed"] == 5


@pytest.mark.parametrize("mode", ["mtproto", "web"])
def test_native_sigint_before_first_finished_attempt_carries_no_synthetic_results(mode):
    script = Path(__file__).parent / "fixtures" / "interruption_child.py"
    result = subprocess.run(
        [sys.executable, "-u", str(script), mode, "--empty"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(
        next(
            line.removeprefix("REPORT:") for line in result.stdout.splitlines() if line.startswith("REPORT:")
        )
    )
    assert report["exception"] == "ScanInterrupted"
    assert report["rows"] == []
    assert report["opened"] == report["closed"] == 2


@pytest.mark.parametrize("mode", ["mtproto", "telegram", "web"])
def test_native_sigint_cli_publishes_observed_top10_and_preserves_last_completed_generation(mode, tmp_path):
    script = Path(__file__).parent / "fixtures" / "interruption_child.py"
    result = subprocess.run(
        [sys.executable, "-u", str(script), mode, str(tmp_path)],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(
        next(
            line.removeprefix("REPORT:") for line in result.stdout.splitlines() if line.startswith("REPORT:")
        )
    )
    assert report["cli_exit"] == 0
    assert report["opened"] == report["closed"] == 5
    folder = tmp_path / "results" / mode
    snapshot = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
    top10 = json.loads((folder / "top10.json").read_text(encoding="utf-8"))
    readable = (folder / "best10_links.txt").read_text(encoding="utf-8")
    assert snapshot["summary"]["interrupted"] is True
    assert {
        field: snapshot["summary"][field]
        for field in ("selected", "tested", "skipped", "fully_tested", "partial_tested", "retry_count")
    } == {"selected": 4, "tested": 2, "skipped": 2, "fully_tested": 1, "partial_tested": 1, "retry_count": 2}
    assert [(row["server"], row["attempts"], row["successes"]) for row in top10] == [
        ("partial.example", 1, 1),
        ("completed.example", 2, 1),
    ]
    assert snapshot["rows"] == top10
    assert "partial.example" in result.stdout and "completed.example" in result.stdout
    assert readable.index("partial.example") < readable.index("completed.example")
    assert "untested.example" not in readable and "never-started.example" not in readable
    if mode == "telegram":
        assert all(row["web_link"].startswith("https://t.me/proxy?") for row in top10)
        assert all(row["secret"] == "00112233445566778899aabbccddeeff" for row in top10)
        assert all("protocol" not in row and "uri" not in row for row in top10)
        assert "https://t.me/proxy?" in readable and "https://t.me/proxy?" in result.stdout
        assert "tg://proxy?" not in readable
    assert (folder / "completed_snapshot.json").read_bytes() == b'{"prior":"last completed generation"}'
