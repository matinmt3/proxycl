import asyncio

import httpx
import pytest

from collector.collector import Collector
from config.loader import AppConfig, SourceConfig

KEY = "00112233445566778899aabbccddeeff"


@pytest.mark.asyncio
async def test_http_collection_isolates_failed_sources_and_deduplicates(monkeypatch, caplog):
    calls = []

    async def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/broken":
            return httpx.Response(503)
        if request.url.path == "/json":
            return httpx.Response(200, json=[{"server": "1.1.1.1", "port": 443, "secret": KEY}])
        return httpx.Response(200, text=f"1.1.1.1:443:{KEY}\n2.2.2.2:443:{KEY}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr("collector.collector.httpx.AsyncClient", lambda **kwargs: client)
    config = AppConfig.load()
    config.sources = [
        SourceConfig("bad", "http_txt", "https://feeds.example/broken?token=private-example"),
        SourceConfig("text", "http_txt", "https://feeds.example/text"),
        SourceConfig("json", "http_json", "https://feeds.example/json"),
        SourceConfig("github-json", "github_raw", "https://feeds.example/json"),
        SourceConfig("disabled", "http_txt", "https://feeds.example/disabled", enabled=False),
    ]
    collector = Collector(config)
    proxies = await collector.collect_async()
    assert [proxy.server for proxy in proxies] == ["1.1.1.1", "2.2.2.2"]
    assert "/disabled" not in calls
    assert "HTTP 503" in caplog.text
    assert "private-example" not in caplog.text
    assert getattr(collector, "source_reports", None) == [
        {"name": "bad", "status": "error", "count": 0, "error": "HTTPStatusError (HTTP 503)"},
        {"name": "text", "status": "ok", "count": 2},
        {"name": "json", "status": "ok", "count": 1},
        {"name": "github-json", "status": "ok", "count": 1},
    ]


@pytest.mark.asyncio
async def test_collection_reads_utf8_bom_file(tmp_path):
    feed = tmp_path / "my proxies.txt"
    feed.write_text(f"1.1.1.1:443:{KEY}", encoding="utf-8-sig")
    config = AppConfig.load()
    config.sources = [SourceConfig("local", "http_txt", "file://" + str(feed))]
    assert len(await Collector(config).collect_async()) == 1


@pytest.mark.asyncio
async def test_oversized_http_and_local_sources_are_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr("collector.collector.MAX_SOURCE_BYTES", 32)
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"x" * 33))
    )
    async with client:
        with pytest.raises(ValueError, match="download limit"):
            await Collector(AppConfig.load())._fetch_url(client, "https://feed.example/list")
    feed = tmp_path / "list.txt"
    feed.write_bytes(b"x" * 33)
    with pytest.raises(ValueError, match="size limit"):
        await Collector(AppConfig.load())._read_local_file("file://" + str(feed))


@pytest.mark.asyncio
async def test_collection_cancellation_is_not_swallowed(monkeypatch):
    async def cancelled(*args, **kwargs):
        raise asyncio.CancelledError

    collector = Collector(AppConfig.load())
    monkeypatch.setattr(collector, "_fetch_url", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await collector._fetch_source(None, SourceConfig("test", "http_txt", "https://feed.example/list"))


@pytest.mark.asyncio
async def test_empty_and_missing_local_files_have_distinct_safe_reports_and_reset(tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("not a proxy", encoding="utf-8")
    config = AppConfig.load()
    config.sources = [
        SourceConfig("empty", "http_txt", "file://" + str(empty)),
        SourceConfig("missing", "http_txt", "file://" + str(tmp_path / "private-secret.txt")),
    ]
    collector = Collector(config)
    assert await collector.collect_async() == []
    assert getattr(collector, "source_reports", None) == [
        {"name": "empty", "status": "empty", "count": 0},
        {"name": "missing", "status": "error", "count": 0, "error": "FileNotFoundError"},
    ]
    config.sources = []
    assert await collector.collect_async() == []
    assert collector.source_reports == []


@pytest.mark.asyncio
async def test_reports_follow_source_order_while_concurrent_reads_remain_bounded(monkeypatch):
    active = peak = 0

    async def respond(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        index = int(request.url.path.removeprefix("/"))
        await asyncio.sleep(0.01 if index % 2 else 0.03)
        active -= 1
        return httpx.Response(200, text=f"1.1.1.1:443:{KEY}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr("collector.collector.httpx.AsyncClient", lambda **kwargs: client)
    config = AppConfig.load()
    config.sources = [
        SourceConfig(f"feed-{index}", "http_txt", f"https://feed.example/{index}") for index in range(21)
    ]
    collector = Collector(config)
    proxies = await collector.collect_async()
    assert len(proxies) == 1
    assert 1 < peak <= 8
    reports = getattr(collector, "source_reports", None)
    assert reports is not None
    assert [r["name"] for r in reports] == [f"feed-{index}" for index in range(21)]
    assert all(r["status"] == "ok" and r["count"] == 1 for r in reports)
