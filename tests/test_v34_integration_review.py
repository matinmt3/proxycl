"""Cross-module regressions for real local feeds, stop tokens and WEB Resume."""

import asyncio
import json
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

import main
from config.loader import AppConfig, SourceConfig, WebSourceConfig
from scan import mode_folder, run_scan
from telegram_webproxy.models import TelegramWebProxy
from utils.models import FailureReason, Proxy, TestSample
from utils.scan_control import ScanController
from utils.scan_state import HistoryStore
from webproxy.models import WebProxy


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\nwebproxy_sources: []\n")
    cfg = AppConfig.load(path)
    cfg.scan.finalist_reconnect_delay_seconds = 0
    cfg.scan.finalist_ping_interval_seconds = 0.1
    cfg.scan.finalists = 1
    cfg.testing.workers = cfg.testing.mtproto_workers = 1
    return cfg


def success(kwargs, latency=100):
    return TestSample(
        True,
        total_latency_ms=1000,
        verification_level="telegram_session",
        dc_id=kwargs["dc_id"],
        ping_rtts_ms=[latency] * kwargs["ping_count"],
    )


def local_web_feed(config, tmp_path):
    proxies = [TelegramWebProxy("one.example/relay", "a" * 32), TelegramWebProxy("two.example", "b" * 32)]
    path = tmp_path / "WEB manual links.txt"
    path.write_text("\n".join(proxy.web_link() for proxy in proxies))
    config.webproxy_sources = [SourceConfig("local WEB", "http_text", "file://" + str(path))]
    return proxies


def install_probes(monkeypatch, strict):
    async def screen(proxy, **kwargs):
        return TestSample(True)

    monkeypatch.setattr("tester.staged.test_proxy_once", screen)
    monkeypatch.setattr("tester.staged.test_proxy_strict", strict)


def test_local_webproxy_feed_runs_all_stages_and_exports_only_sustained_links(config, tmp_path, monkeypatch):
    proxies = local_web_feed(config, tmp_path)

    async def strict(proxy, **kwargs):
        return success(kwargs, 100 if proxy.server.startswith("one") else 200)

    install_probes(monkeypatch, strict)
    outcome = run_scan(config, "webproxy")
    assert [row.proxy.key() for row in outcome.results] == [row.key() for row in proxies]
    assert [row.proxy.key() for row in outcome.top] == [proxies[0].key()]
    assert outcome.summary["finalist_completed"] == 1 and not outcome.summary["interrupted"]
    assert outcome.source_reports[0]["strict_qualified"] == 1
    rows = json.loads((outcome.folder / "proxy.json").read_text())
    assert len(rows) == 1 and rows[0]["kind"] == rows[0]["protocol"] == "webproxy"
    assert rows[0]["ping_count"] == 59
    assert (outcome.folder / "proxy.txt").read_text().strip() == proxies[0].tg_link()
    history = HistoryStore(Path(config.output.folder) / ".state", "default", "webproxy").rows()
    assert history[0]["kind"] == "webproxy"


def test_webproxy_recheck_uses_saved_kind_protocol_and_only_saved_feed(config, tmp_path, monkeypatch):
    proxies = local_web_feed(config, tmp_path)

    async def strict(proxy, **kwargs):
        return success(kwargs, 100 if proxy.server.startswith("one") else 200)

    install_probes(monkeypatch, strict)
    outcome = run_scan(config, "webproxy")
    original_sources = list(config.webproxy_sources)
    calls = []

    def check(prepared, **kwargs):
        from collector.telegram_webproxy import TelegramWebCollector

        candidates = TelegramWebCollector(prepared).collect()
        calls.append((candidates, kwargs, prepared._scan_command))
        return True

    monkeypatch.setattr(main, "cmd_best10", check)
    assert main.cmd_recheck(config, mode="webproxy") is True
    assert [proxy.key() for proxy in calls[0][0]] == [outcome.top[0].proxy.key()]
    assert calls[0][1:] == ({"mode": "webproxy", "limit": None}, "recheck")
    assert config.webproxy_sources == original_sources
    assert (outcome.folder / "recheck-candidates.txt").read_text().strip() == proxies[0].tg_link()


@pytest.mark.parametrize("mode", ["mtproto", "web", "webproxy"])
def test_controller_stop_during_collection_preserves_prior_snapshot(config, tmp_path, monkeypatch, mode):
    from collector.collector import Collector
    from webproxy.collector import WebCollector

    config.sources = [SourceConfig("waiting", "http_text", "https://feed.invalid/list")]
    config.web_sources = [WebSourceConfig("waiting", "http_text", "https://feed.invalid/list")]
    config.webproxy_sources = [SourceConfig("waiting", "http_text", "https://feed.invalid/list")]
    controller = ScanController()
    config._scan_controller = controller
    folder = mode_folder(config, mode)
    folder.mkdir(parents=True)
    snapshot = folder / "snapshot.json"
    snapshot.write_text('{"previous":"preserved"}')

    async def slow_feed(*args, **kwargs):
        await asyncio.sleep(0.3)
        return []

    async def slow_web_url(*args, **kwargs):
        await asyncio.sleep(0.3)
        return ""

    monkeypatch.setattr(Collector, "_fetch_source", slow_feed)
    monkeypatch.setattr(WebCollector, "_fetch_url", slow_web_url)
    monkeypatch.setattr("collector.telegram_webproxy.TelegramWebCollector._fetch_source", slow_feed)
    timer = threading.Timer(0.03, controller.request_stop)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(KeyboardInterrupt, match="Collection stopped"):
            run_scan(config, mode)
    finally:
        timer.cancel()
        timer.join()
    assert time.monotonic() - started < 0.25
    assert snapshot.read_text() == '{"previous":"preserved"}'
    assert not list((Path(config.output.folder) / ".state").glob("checkpoint-*"))


@pytest.mark.parametrize("mode", ["mtproto", "webproxy"])
def test_stale_resume_keeps_pool_and_run_id_but_discards_all_old_samples(config, monkeypatch, mode):
    from collector.collector import Collector
    from collector.telegram_webproxy import TelegramWebCollector

    proxy = (
        TelegramWebProxy("saved.example/path", "a" * 32)
        if mode == "webproxy"
        else Proxy("saved.example", 443, "a" * 32)
    )
    cls = TelegramWebCollector if mode == "webproxy" else Collector
    monkeypatch.setattr(cls, "collect", lambda self: [proxy])
    calls = 0

    async def first_probe(proxy, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise asyncio.CancelledError
        return success(kwargs)

    install_probes(monkeypatch, first_probe)
    first = run_scan(config, mode)
    assert first.summary["interrupted"] and first.results[0].attempts == 1
    checkpoint = Path(config.output.folder) / ".state" / f"checkpoint-{mode}-default.json"
    row = json.loads(checkpoint.read_text())
    row["state"]["engine"]["results"][0]["samples"][0]["timestamp"] -= 7200
    row["state"]["context"]["started_at"] = datetime.fromtimestamp(
        time.time() - 7200, timezone.utc
    ).isoformat()
    checkpoint.write_text(json.dumps(row))
    observed = []

    async def resumed_probe(proxy, **kwargs):
        observed.append(kwargs["dc_id"])
        return success(kwargs)

    monkeypatch.setattr(cls, "collect", lambda self: pytest.fail("Resume refetched sources"))
    install_probes(monkeypatch, resumed_probe)
    config.scan.resume = True
    resumed = run_scan(config, mode)
    assert resumed.summary["run_id"] == first.summary["run_id"]
    assert resumed.summary["resume_refresh"]["stale_candidates"] == 1
    assert len(observed) == 8 and resumed.results[0].attempts == 8
    history = HistoryStore(Path(config.output.folder) / ".state", "default", mode).rows()
    assert history[0]["run_count"] == 1 and history[0]["historical_attempts"] == 8


def test_complete_rejected_screening_reports_complete_candidate_coverage(config, monkeypatch):
    from collector.collector import Collector

    config.scan.time_budget_seconds = 3
    proxies = [Proxy(f"rejected{index}.example", 443, "a" * 32) for index in range(3)]
    monkeypatch.setattr(Collector, "collect", lambda self: proxies)

    async def reject(*args, **kwargs):
        return TestSample(False, failure_reason=FailureReason.TCP_REFUSED)

    monkeypatch.setattr("tester.staged.test_proxy_once", reject)
    outcome = run_scan(config)
    assert outcome.summary["prefilter_tested"] == 3 and outcome.summary["tested"] == 0
    assert not outcome.summary["interrupted"] and outcome.summary["unexamined_candidates"] == 0
    assert outcome.summary["coverage_percent"] == 100.0


def test_timebox_resume_keeps_finished_finalists_and_pending_pool_separate(config, monkeypatch):
    from collector.collector import Collector

    config.scan.time_budget_seconds = 0.15
    proxies = [Proxy(f"proxy{index}.example", 443, "a" * 32) for index in range(3)]
    monkeypatch.setattr(Collector, "collect", lambda self: proxies)

    async def first_probe(proxy, **kwargs):
        if proxy.server != "proxy0.example" and kwargs["ping_count"] == 3:
            await asyncio.Event().wait()
        return success(kwargs)

    install_probes(monkeypatch, first_probe)
    stopped = run_scan(config)
    assert stopped.summary["interrupted"] and stopped.summary["budget_exhausted"]
    assert stopped.summary["stop_reason"] == "budget"
    assert stopped.top[0].attempts == 8 and stopped.summary["coverage_limited"]
    assert stopped.summary["skipped"] == 2
    resumed_calls = Counter()

    async def resumed_probe(proxy, **kwargs):
        resumed_calls[proxy.key()] += 1
        return success(kwargs)

    install_probes(monkeypatch, resumed_probe)
    config.scan.resume = True
    config.scan.time_budget_seconds = None
    resumed = run_scan(config)
    assert resumed_calls[proxies[0].key()] == 0
    assert [result.attempts for result in resumed.results] == [8, 8, 3]
    assert not resumed.summary["interrupted"] and not resumed.summary["coverage_limited"]


@pytest.mark.parametrize(
    "mode,strategy", [("mtproto", "staged"), ("mtproto", "single"), ("web", "single"), ("webproxy", "staged")]
)
def test_initial_checkpoint_time_does_not_grant_scanner_a_second_time_budget(
    config, monkeypatch, mode, strategy
):
    from collector.collector import Collector
    from collector.telegram_webproxy import TelegramWebCollector
    from utils.scan_state import CheckpointStore
    from webproxy.collector import WebCollector

    config.scan.time_budget_seconds = 0.2
    config.scan.strategy = strategy
    proxy = (
        WebProxy("saved.example", 80)
        if mode == "web"
        else (
            TelegramWebProxy("saved.example/path", "a" * 32)
            if mode == "webproxy"
            else Proxy("saved.example", 443, "a" * 32)
        )
    )
    cls = WebCollector if mode == "web" else TelegramWebCollector if mode == "webproxy" else Collector
    monkeypatch.setattr(cls, "collect", lambda self: [proxy])
    original_save = CheckpointStore.save
    saved = 0

    def slow_first_save(self, candidates, state):
        nonlocal saved
        saved += 1
        if saved == 1:
            time.sleep(0.3)
        return original_save(self, candidates, state)

    monkeypatch.setattr(CheckpointStore, "save", slow_first_save)
    calls = []

    async def strict(proxy, *args, **kwargs):
        calls.append(proxy.key())
        return TestSample(True, total_latency_ms=100) if mode == "web" else success(kwargs)

    async def screen(proxy, **kwargs):
        calls.append(proxy.key())
        return TestSample(True)

    install_probes(monkeypatch, strict)
    monkeypatch.setattr("tester.staged.test_proxy_once", screen)
    monkeypatch.setattr("tester.mtproto.test_proxy_strict", strict)
    monkeypatch.setattr("webproxy.tester.test_proxy_once", strict)
    outcome = run_scan(config, mode)
    assert calls == []
    assert outcome.summary["interrupted"] and outcome.summary["budget_exhausted"]
    assert outcome.summary["selected"] == 1 and outcome.summary["tested"] == 0
