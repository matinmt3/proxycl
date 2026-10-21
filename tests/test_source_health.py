"""Persistent source health is local metadata, never feed URLs or proxy secrets."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from collector.collector import Collector
from collector.health import SourceHealth
from config.loader import AppConfig, SourceConfig, WebSourceConfig
from tests.proxy_fixtures import qualified_result
from utils.models import Proxy, ProxyResult, TestSample
from webproxy.collector import WebCollector
from webproxy.models import WebProxy

KEY = "00112233445566778899aabbccddeeff"


def config_for(tmp_path, network="wifi"):
    config = AppConfig.load()
    config.output.folder = str(tmp_path / "output")
    config.scan = SimpleNamespace(
        source_health_enabled=True,
        source_failure_threshold=3,
        source_cooldown_minutes=30,
        network=network,
    )
    return config


def result_report(name, status="error", count=0):
    return {"name": name, "status": status, "count": count}


def test_three_network_failures_cool_down_and_expiry_retries_then_recovers(tmp_path):
    now = [1000.0]
    config = config_for(tmp_path)
    source = SourceConfig("private feed", "http_txt", "https://user:password@example.test/feed?token=secret")
    health = SourceHealth(config, "mtproto", clock=lambda: now[0])
    for attempt in range(3):
        assert health.cooldown_report(source) is None
        health.record_collection([source], [result_report(source.name)], [True])
        assert health.rows([source])[0]["consecutive_failures"] == attempt + 1
    report = health.cooldown_report(source)
    assert report["status"] == "error"
    assert report["cooldown_until"] == 2800
    assert "Cooling down" in report["error"]
    serialized = health.path.read_text(encoding="utf-8")
    assert all(private not in serialized for private in ("https://", "password", "token", "secret"))
    now[0] = 2800
    restored = SourceHealth(config, "mtproto", clock=lambda: now[0])
    assert restored.cooldown_report(source) is None
    restored.record_collection([source], [result_report(source.name, "ok", 5)], [False])
    row = restored.rows([source])[0]
    assert row["consecutive_failures"] == 0
    assert row["next_retry"] is None
    assert row["status"] == "ok" and row["count"] == 5


def test_empty_parse_errors_and_manual_sources_do_not_quarantine(tmp_path):
    health = SourceHealth(config_for(tmp_path), "mtproto", clock=lambda: 1000.0)
    empty = SourceConfig("empty", "http_txt", "https://example.test/empty")
    invalid = SourceConfig("invalid", "http_txt", "https://example.test/invalid")
    manual = SourceConfig("manual", "http_txt", "file://missing.txt")
    for _ in range(6):
        health.record_collection(
            [empty, invalid, manual],
            [result_report("empty", "empty"), result_report("invalid"), result_report("manual")],
            [False, False, True],
        )
    assert all(health.cooldown_report(source) is None for source in (empty, invalid, manual))
    assert [row["consecutive_failures"] for row in health.rows([empty, invalid, manual])] == [0, 0, 0]


def test_cooldown_skips_do_not_extend_failure_or_check_time_and_empty_resets_streak(tmp_path):
    now = [1000.0]
    config = config_for(tmp_path)
    config.scan.source_failure_threshold = 2
    config.scan.source_cooldown_minutes = 1
    health = SourceHealth(config, "mtproto", clock=lambda: now[0])
    source = SourceConfig("feed", "http_txt", "https://example.test/list")
    for _ in range(2):
        health.record_collection([source], [result_report("feed")], [True])
    now[0] = 1020
    skip = health.cooldown_report(source)
    health.record_collection([source], [skip], [False])
    row = health.rows([source])[0]
    assert row["consecutive_failures"] == 2
    assert row["last_checked"] == 1000
    assert row["next_retry"] == 1060
    now[0] = 1060
    health.record_collection([source], [result_report("feed", "empty")], [False])
    assert health.rows([source])[0]["consecutive_failures"] == 0
    health.record_collection([source], [result_report("feed")], [True])
    assert health.cooldown_report(source) is None


def test_feed_url_used_as_name_is_redacted_and_identity_stays_after_rename(tmp_path):
    health = SourceHealth(config_for(tmp_path), "mtproto")
    url = "https://user:password@example.test/list?token=secret"
    source = SourceConfig(url, "http_txt", url)
    health.record_collection([source], [result_report(source.name, "ok", 1)], [False])
    assert health.rows([source])[0]["name"] == "[feed]"
    source.name = "new name"
    assert health.rows([source])[0]["count"] == 1
    assert "password" not in health.path.read_text(encoding="utf-8")


def test_state_is_isolated_by_network_mode_and_source_type(tmp_path):
    source = SourceConfig("feed", "http_txt", "https://example.test/list")
    config = config_for(tmp_path)
    health = SourceHealth(config, "mtproto", clock=lambda: 1000.0)
    for _ in range(3):
        health.record_collection([source], [result_report(source.name)], [True])
    assert health.cooldown_report(source)
    assert SourceHealth(config, "web").cooldown_report(source) is None
    assert SourceHealth(config_for(tmp_path, "cellular"), "mtproto").cooldown_report(source) is None
    changed = SourceConfig("feed", "http_json", source.url)
    assert health.cooldown_report(changed) is None


@pytest.mark.parametrize("network", ["../../outside", "CON", "LPT1"])
def test_path_traversal_network_is_namespaced_safely(tmp_path, network):
    config = config_for(tmp_path, network)
    health = SourceHealth(config, "mtproto")
    assert health.path.is_relative_to(tmp_path / "output" / ".state")
    assert ".." not in health.path.parts
    assert health.path.parent.parent.name != network


def test_bad_feed_url_does_not_break_health_reporting(tmp_path):
    health = SourceHealth(config_for(tmp_path), "mtproto")
    source = SourceConfig("feed", "http_txt", "https://[")
    health.record_collection([source], [result_report("feed")], [False])
    assert health.rows([source])[0]["status"] == "error"


def test_corrupt_state_and_write_errors_warn_without_stopping(tmp_path, monkeypatch, caplog):
    config = config_for(tmp_path)
    health = SourceHealth(config, "mtproto")
    health.path.parent.mkdir(parents=True)
    health.path.write_text("not json", encoding="utf-8")
    loaded = SourceHealth(config, "mtproto")
    source = SourceConfig("feed", "http_txt", "https://example.test/list?secret=hidden")
    monkeypatch.setattr(loaded, "_save", lambda: (_ for _ in ()).throw(OSError("hidden secret")))
    loaded.record_collection([source], [result_report("feed", "ok", 2)], [False])
    assert loaded.rows([source])[0]["count"] == 2
    assert "OSError" in caplog.text
    assert "hidden" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mtproto", "web"])
async def test_collector_cooldown_and_recovery_are_persistent_and_ordered(tmp_path, monkeypatch, mode):
    config = config_for(tmp_path)
    source_class = WebSourceConfig if mode == "web" else SourceConfig
    sources = [
        source_class("broken", "http_txt", "https://feed.test/broken?token=hidden"),
        source_class("good", "http_txt", "https://feed.test/good"),
    ]
    setattr(config, "web_sources" if mode == "web" else "sources", sources)
    cls = WebCollector if mode == "web" else Collector
    calls = []
    recovering = [False]

    async def fetch(self, client, url):
        calls.append(url)
        if "broken" in url and not recovering[0]:
            raise httpx.ConnectError("private: hidden")
        return "1.1.1.1:8080" if mode == "web" else f"1.1.1.1:443:{KEY}"

    monkeypatch.setattr(cls, "_fetch_url", fetch)
    for _ in range(3):
        collector = cls(config)
        assert len(await collector.collect_async()) == 1
        assert [row["name"] for row in collector.source_reports] == ["broken", "good"]
    collector = cls(config)
    assert len(await collector.collect_async()) == 1
    assert len(calls) == 7
    assert "Cooling down" in collector.source_reports[0]["error"]
    assert collector.health_rows[0]["consecutive_failures"] == 3
    collector.health._clock = lambda: 10**12
    recovering[0] = True
    assert len(await collector.collect_async()) == 1
    assert collector.source_reports[0]["status"] == "ok"
    assert collector.health_rows[0]["consecutive_failures"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mtproto", "web"])
async def test_cancelled_collection_does_not_publish_or_persist_partial_health(tmp_path, monkeypatch, mode):
    config = config_for(tmp_path)
    source_class = WebSourceConfig if mode == "web" else SourceConfig
    setattr(
        config,
        "web_sources" if mode == "web" else "sources",
        [
            source_class("bad", "http_txt", "https://feed.test/bad"),
            source_class("cancel", "http_txt", "https://feed.test/cancel"),
        ],
    )
    cls = WebCollector if mode == "web" else Collector

    async def fetch(self, client, url):
        if url.endswith("bad"):
            raise httpx.ConnectError("private")
        await asyncio.sleep(0.01)
        raise asyncio.CancelledError

    monkeypatch.setattr(cls, "_fetch_url", fetch)
    collector = cls(config)
    with pytest.raises(asyncio.CancelledError):
        await collector.collect_async()
    assert collector.source_reports == []
    assert not collector.health.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["mtproto", "web"])
async def test_quality_counts_only_first_deduplicated_source_and_finished_results(
    tmp_path, monkeypatch, mode
):
    config = config_for(tmp_path)
    source_class = WebSourceConfig if mode == "web" else SourceConfig
    setattr(
        config,
        "web_sources" if mode == "web" else "sources",
        [
            source_class("first", "http_txt", "https://feed.test/first"),
            source_class("second", "http_txt", "https://feed.test/second"),
        ],
    )
    cls = WebCollector if mode == "web" else Collector

    async def fetch(self, client, url):
        return "1.1.1.1:8080" if mode == "web" else f"1.1.1.1:443:{KEY}"

    monkeypatch.setattr(cls, "_fetch_url", fetch)
    collector = cls(config)
    proxy = (await collector.collect_async())[0]
    result = (
        ProxyResult(proxy, [TestSample(True, total_latency_ms=50)])
        if mode == "web"
        else qualified_result(proxy)
    )
    unknown = (
        ProxyResult(WebProxy("2.2.2.2", 8080)) if mode == "web" else ProxyResult(Proxy("2.2.2.2", 443, KEY))
    )
    reports = collector.record_quality([result, result, unknown])
    assert [r["checked"] for r in reports] == [1, 0]
    assert [r["qualified"] for r in reports] == [1, 0]
    assert [r["strict_qualified"] for r in reports] == ([0, 0] if mode == "web" else [1, 0])
    assert reports[0]["quality_attribution"] == "first_source_after_deduplication"
    restored = SourceHealth(config, mode)
    rows = restored.rows(getattr(config, "web_sources" if mode == "web" else "sources"))
    assert rows[0]["checked"] == 1 and rows[1]["checked"] == 0
    assert KEY not in restored.path.read_text(encoding="utf-8")


def test_disabled_health_writes_no_state(tmp_path):
    config = config_for(tmp_path)
    config.scan.source_health_enabled = False
    health = SourceHealth(config, "mtproto")
    source = SourceConfig("feed", "http_txt", "https://feed.test/list")
    for _ in range(5):
        health.record_collection([source], [result_report("feed")], [True])
    assert health.cooldown_report(source) is None
    assert not health.path.exists()
