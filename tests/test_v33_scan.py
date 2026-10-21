import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from config.loader import AppConfig
from scan import run_scan
from utils.models import FailureReason, Proxy, TestSample
from utils.scan_state import HistoryStore


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\n")
    cfg = AppConfig.load(path)
    cfg.scan.finalist_reconnect_delay_seconds = 0
    cfg.scan.finalist_ping_interval_seconds = 0.1
    cfg.scan.finalists = 2
    return cfg


def proxy(name):
    return Proxy(name + ".example", 443, "01" * 16)


def successful(kwargs, latency=100):
    return TestSample(
        True,
        total_latency_ms=1000,
        verification_level="telegram_session",
        dc_id=kwargs["dc_id"],
        ping_rtts_ms=[latency] * kwargs["ping_count"],
    )


def test_default_staged_scan_filters_fast_unstable_and_baseline_only_results(config, monkeypatch):
    from collector.collector import Collector
    from tester import staged

    candidates = [proxy(name) for name in ("dead", "unstable", "good", "baseline-only")]
    monkeypatch.setattr(Collector, "collect", lambda self: candidates)
    calls = Counter()

    async def prefilter(p, **kwargs):
        return TestSample(
            p.server != "dead.example",
            failure_reason=FailureReason.NONE if p.server != "dead.example" else FailureReason.TCP_REFUSED,
        )

    async def strict(p, **kwargs):
        calls[p.server] += 1
        if p.server == "unstable.example" and calls[p.server] > 3:
            return TestSample(False, failure_reason=FailureReason.HANDSHAKE_TIMEOUT)
        return successful(
            kwargs, {"unstable.example": 10, "good.example": 100, "baseline-only.example": 300}[p.server]
        )

    monkeypatch.setattr(staged, "test_proxy_once", prefilter)
    monkeypatch.setattr(staged, "test_proxy_strict", strict)
    result = run_scan(config)
    assert result.summary["prefilter_tested"] == 4 and result.summary["prefilter_passed"] == 3
    assert result.summary["finalist_completed"] == 2 and result.summary["finalist_attempts"] == 10
    assert [row.proxy.server for row in result.top] == ["good.example"]
    assert calls == {"unstable.example": 8, "good.example": 8, "baseline-only.example": 3}
    assert result.top[0].quality_policy.min_successes == 7
    snapshot = json.loads((result.folder / "snapshot.json").read_text())
    assert snapshot["rows"][0]["ping_count"] == 59
    assert snapshot["summary"]["provisional"] is False
    observations = json.loads((result.folder / "observations.json").read_text())
    assert len(observations["prefilter_results"]) == 4
    assert len(observations["rows"]) == 3
    with pytest.raises(ValueError, match="already complete"):
        config.scan.resume = True
        run_scan(config)


@pytest.mark.parametrize("mode", ["mtproto", "telegram"])
def test_stopped_staged_scan_resumes_saved_attempts_and_history_is_not_double_counted(
    config, monkeypatch, mode
):
    from collector.collector import Collector
    from tester import staged

    candidate = proxy("good")
    monkeypatch.setattr(Collector, "collect", lambda self: [candidate])
    calls = []

    async def prefilter(p, **kwargs):
        return TestSample(True)

    async def strict(p, **kwargs):
        calls.append(kwargs["dc_id"])
        if len(calls) == 6:
            raise asyncio.CancelledError
        return successful(kwargs)

    monkeypatch.setattr(staged, "test_proxy_once", prefilter)
    monkeypatch.setattr(staged, "test_proxy_strict", strict)
    first = run_scan(config, mode)
    assert first.summary["interrupted"] and first.summary["provisional"]
    assert first.top[0].attempts == 5
    monkeypatch.setattr(Collector, "collect", lambda self: pytest.fail("resume fetched sources"))
    config.scan.resume = True
    second = run_scan(config, mode)
    assert second.summary["resumed"] and not second.summary["interrupted"]
    assert second.summary["run_id"] == first.summary["run_id"]
    assert second.top[0].attempts == 8 and len(calls) == 9
    history = HistoryStore(Path(config.output.folder) / ".state", "default", mode).rows()
    assert history[0]["historical_attempts"] == 8 and history[0]["run_count"] == 1
    assert json.loads((second.folder / "progress.json").read_text())["active"] is False


def test_named_network_staged_benchmark_does_not_create_state_or_outputs(config, monkeypatch):
    from collector.collector import Collector

    config.scan.network = "mobile"
    monkeypatch.setattr(Collector, "collect", lambda self: [])
    result = run_scan(config, export=False)
    assert result.summary["network"] == "mobile"
    assert not Path(config.output.folder).exists()
