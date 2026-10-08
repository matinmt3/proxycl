import json
from types import SimpleNamespace

import pytest


def test_web_result_keeps_ipv6_protocol_without_telegram_fields():
    from utils.models import ProxyResult, TestSample
    from webproxy.models import WebProxy

    proxy = WebProxy(" [2001:0db8::1] ", 1080, "socks5", "fixture")
    result = ProxyResult(proxy, [TestSample(True, total_latency_ms=10)])
    row = result.to_dict()
    assert proxy.key() == "socks5://[2001:db8::1]:1080"
    assert row["uri"] == "socks5://[2001:db8::1]:1080"
    assert row["protocol"] == "socks5"
    assert "secret" not in row and "tg_link" not in row


@pytest.mark.parametrize(
    "host,port,protocol",
    [
        ("bad host", 80, "http"),
        ("999.1.1.1", 80, "http"),
        ("host", 0, "http"),
        ("host", True, "http"),
        ("host", 80, "mtproto"),
    ],
)
def test_invalid_web_endpoint_cannot_be_constructed(host, port, protocol):
    from webproxy.models import WebProxy

    with pytest.raises(ValueError):
        WebProxy(host, port, protocol)


def test_text_parser_preserves_explicit_transport_and_rejects_credentials_and_url_payloads():
    from webproxy.sources import parse_text_blob

    text = "\ufeff# feed\n127.0.0.1:80\nhttp://127.0.0.1:80\nhttps://proxy.example:443\nsocks4://[2001:db8::1]:1080\nsocks5://proxy.example:1081\nuser:password@proxy.example:1080\nhttp://u:p@proxy.example:80\nhttp://proxy.example:80/path\nhttp://proxy.example:80?secret=x\n999.1.1.1:80\n127.0.0.1:65536\nhtml <span>127.0.0.1:81</span>"
    parsed = parse_text_blob(text, "fixture", "http")
    assert [p.uri() for p in parsed] == [
        "http://127.0.0.1:80",
        "https://proxy.example:443",
        "socks4://[2001:db8::1]:1080",
        "socks5://proxy.example:1081",
    ]
    assert all(p.source == "fixture" for p in parsed)


def test_bare_https_capability_feed_uses_supplied_http_transport():
    from webproxy.sources import parse_text_blob

    assert [p.uri() for p in parse_text_blob("proxy.example:443\n", "https-capable", "http")] == [
        "http://proxy.example:443"
    ]


def test_json_parser_handles_objects_and_uri_lists_without_importing_credentials():
    from webproxy.sources import parse_json_feed

    data = {
        "proxies": [
            {"ip": "192.0.2.1", "port": 8080, "protocol": "http", "https": True},
            {"host": "proxy.example", "port": 1080, "protocol": "socks5"},
            {
                "host": "secret.example",
                "port": 1080,
                "protocol": "socks5",
                "username": "alice",
                "password": "private",
            },
            "socks4://192.0.2.2:1080",
        ]
    }
    assert [p.uri() for p in parse_json_feed(json.dumps(data), "fixture")] == [
        "http://192.0.2.1:8080",
        "socks5://proxy.example:1080",
        "socks4://192.0.2.2:1080",
    ]


@pytest.mark.asyncio
async def test_collection_isolates_bad_empty_and_disabled_sources(tmp_path):
    from webproxy.collector import WebCollector

    good = tmp_path / "good.txt"
    good.write_text("192.0.2.1:80\n192.0.2.1:80\n", encoding="utf-8")
    empty = tmp_path / "empty.txt"
    empty.write_text("not a proxy\n", encoding="utf-8")

    def source(name, path, enabled=True):
        return SimpleNamespace(
            name=name, url="file://" + str(path), type="http_txt", protocol="http", enabled=enabled
        )

    config = SimpleNamespace(
        web_sources=[
            source("bad", tmp_path / "missing-secret.txt"),
            source("good", good),
            source("empty", empty),
            source("disabled", good, False),
        ]
    )
    collector = WebCollector(config)
    result = await collector.collect_async()
    assert [p.uri() for p in result] == ["http://192.0.2.1:80"]
    assert collector.source_reports == [
        {"name": "bad", "status": "error", "count": 0, "error": "FileNotFoundError"},
        {"name": "good", "status": "ok", "count": 1},
        {"name": "empty", "status": "empty", "count": 0},
    ]
    assert "missing-secret" not in json.dumps(collector.source_reports)


@pytest.mark.asyncio
async def test_oversized_local_feed_is_rejected_while_good_feed_survives(tmp_path):
    from webproxy.collector import WebCollector

    large = tmp_path / "large.txt"
    large.write_bytes(b"#" * (4 * 1024 * 1024 + 1))
    good = tmp_path / "good.txt"
    good.write_text("192.0.2.2:1080")
    sources = [
        SimpleNamespace(
            name=name, url="file://" + str(path), type="http_txt", protocol="socks4", enabled=True
        )
        for name, path in [("large", large), ("good", good)]
    ]
    collector = WebCollector(SimpleNamespace(web_sources=sources))
    assert [p.uri() for p in await collector.collect_async()] == ["socks4://192.0.2.2:1080"]
    assert collector.source_reports[0] == {
        "name": "large",
        "status": "error",
        "count": 0,
        "error": "ValueError",
    }
