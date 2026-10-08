"""Catalog fallback and explicit overrides must remain independent per mode."""

import json
from pathlib import Path

import pytest

from config.loader import AppConfig


def make_catalog(tmp_path):
    path = tmp_path / "feeds.yaml"
    path.write_text(
        "sources:\n"
        "  - {name: mt, type: http_txt, url: 'file://manual.txt'}\n"
        "web_sources:\n"
        "  - {name: web, type: http_txt, url: 'file://web.txt', protocol: socks5}\n",
        encoding="utf-8",
    )
    return path


def load_settings(tmp_path, text):
    path = tmp_path / "settings.yaml"
    path.write_text(text, encoding="utf-8")
    return AppConfig.load(path)


def test_catalog_supplies_both_modes_and_resolves_local_paths_from_config(tmp_path, monkeypatch):
    make_catalog(tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    config = load_settings(tmp_path, "source_catalog: feeds.yaml\n")
    assert [(s.name, s.url) for s in config.sources] == [("mt", "file://" + str(tmp_path / "manual.txt"))]
    assert [(s.name, s.protocol, s.url) for s in config.web_sources] == [
        ("web", "socks5", "file://" + str(tmp_path / "web.txt"))
    ]


@pytest.mark.parametrize("mode", ["sources", "web_sources"])
def test_explicit_empty_list_overrides_only_its_catalog_mode(tmp_path, mode):
    make_catalog(tmp_path)
    config = load_settings(tmp_path, f"source_catalog: feeds.yaml\n{mode}: []\n")
    assert getattr(config, mode) == []
    other = "web_sources" if mode == "sources" else "sources"
    assert len(getattr(config, other)) == 1


def test_explicit_web_list_overrides_catalog_and_defaults_protocol_to_http(tmp_path):
    make_catalog(tmp_path)
    config = load_settings(
        tmp_path,
        "source_catalog: feeds.yaml\nweb_sources:\n"
        "  - {name: custom, type: http_txt, url: 'https://feeds.example/https.txt'}\n",
    )
    assert [s.name for s in config.sources] == ["mt"]
    assert [(s.name, s.protocol) for s in config.web_sources] == [("custom", "http")]


def test_legacy_external_config_does_not_implicitly_load_project_sources(tmp_path):
    config = load_settings(tmp_path, "sources: []\n")
    assert config.sources == []
    assert config.web_sources == []


def test_standard_file_uri_resolves_for_both_source_modes(tmp_path):
    uri = (tmp_path / "proxy list.txt").as_uri()
    config = load_settings(
        tmp_path,
        f"sources: [{{name: mt, type: http_txt, url: '{uri}'}}]\n"
        f"web_sources: [{{name: web, type: http_txt, url: '{uri}'}}]\n",
    )
    expected = "file://" + str(tmp_path / "proxy list.txt")
    assert config.sources[0].url == expected
    assert config.web_sources[0].url == expected


@pytest.mark.parametrize(
    "content, match",
    [
        ("web_sources: {}", "web_sources must be a YAML list"),
        ("web_sources: [null]", "web_sources\\[1\\] must be a YAML mapping"),
        ("web_sources: [{name: web}]", "Invalid web_sources"),
        (
            "web_sources: [{name: web, type: http_txt, url: 'https://feed.example', protocol: ftp}]",
            "protocol must be",
        ),
        (
            "web_sources: [{name: web, type: telegram_channel, url: 'https://feed.example'}]",
            "Unsupported web source type",
        ),
        (
            "web_sources: [{name: web, type: http_txt, url: 'https://feed.example', enabled: 'false'}]",
            "enabled must be true or false",
        ),
        ("source_catalog: []", "source_catalog must be a non-empty string"),
    ],
)
def test_invalid_catalog_config_reports_the_actual_field(tmp_path, content, match):
    with pytest.raises(ValueError, match=match):
        load_settings(tmp_path, content)


@pytest.mark.parametrize(
    "catalog_text, message",
    [
        ("[]", "catalog must contain a YAML mapping"),
        ("unknown_mode: []", "Unknown catalog section"),
        ("sources: {}", "sources must be a YAML list"),
    ],
)
def test_malformed_catalog_is_rejected(tmp_path, catalog_text, message):
    (tmp_path / "feeds.yaml").write_text(catalog_text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_settings(tmp_path, "source_catalog: feeds.yaml\n")


def test_missing_catalog_has_a_clear_file_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing.yaml"):
        load_settings(tmp_path, "source_catalog: missing.yaml\n")


def test_conventional_config_catalog_path_resolves_from_project_root(tmp_path):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    catalog = make_catalog(config_dir)
    path = config_dir / "config.yaml"
    path.write_text("source_catalog: config/feeds.yaml\n", encoding="utf-8")
    config = AppConfig.load(path)
    assert config.sources[0].url == "file://" + str(tmp_path / "manual.txt")
    assert catalog.exists()


def test_default_catalog_provides_at_least_fifty_distinct_remote_sources_per_mode():
    config = AppConfig.load()
    for sources in (config.sources, config.web_sources):
        urls = [s.url for s in sources if s.enabled and s.url.startswith(("https://", "http://"))]
        assert len(urls) >= 50
        assert len(urls) == len(set(url.lower() for url in urls))
    local = [s for s in config.sources if s.url.startswith("file://")]
    assert len(local) == 1
    assert local[0].url == "file://" + str(Path(__file__).resolve().parents[1] / "config/manual_proxies.txt")


def test_publisher_declared_tls_feed_keeps_tls_for_bare_endpoints():
    from webproxy.sources import parse_text_blob

    config = AppConfig.load()
    source = next(
        s
        for s in config.web_sources
        if s.url == "https://raw.githubusercontent.com/databay-labs/free-proxy-list/master/https.txt"
    )
    proxies = parse_text_blob("198.51.100.3:8443", source.name, source.protocol)
    assert [proxy.uri() for proxy in proxies] == ["https://198.51.100.3:8443"]


def test_default_mtproto_collection_succeeds_when_direct_telegram_access_is_blocked(monkeypatch):
    """Defaults must collect every remote feed without needing the blocked t.me host."""
    import httpx

    from collector.collector import Collector

    fixture_path = Path(__file__).parent / "fixtures" / "mtproto_mirror_samples.json"
    samples = json.loads(fixture_path.read_text(encoding="utf-8"))
    config = AppConfig.load()
    remote = [source for source in config.sources if source.enabled and not source.url.startswith("file://")]
    client_type = httpx.AsyncClient

    def public_feed(request):
        if request.url.host in {"t.me", "telegram.me"}:
            raise httpx.ConnectError("Resolver routes Telegram to a refused private address", request=request)
        captured = samples["feeds"].get(str(request.url))
        if captured is None:
            return httpx.Response(404)
        return httpx.Response(200, text=captured["body"])

    transport = httpx.MockTransport(public_feed)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=transport, **kwargs))
    collector = Collector(config)
    proxies = collector.collect()
    report_by_name = {report["name"]: report for report in collector.source_reports}
    reports = [report_by_name[source.name] for source in remote]
    assert len(reports) >= 50
    assert all(report["status"] == "ok" and report["count"] > 0 for report in reports)
    assert len(proxies) == samples["unique_sample_candidates"]
