import csv
import json
from io import StringIO
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from collector.sources import parse_text_blob
from config.loader import AppConfig
from utils.models import FailureReason, Proxy, ProxyResult, TestSample

USER_LINK = "https://t.me/proxy?server=135.181.74.178&port=443&secret=3XnnAQIAAQAH8AMDhuJMOt0"


def test_user_telegram_web_link_round_trips_without_changing_secret_bytes():
    proxy = parse_text_blob(USER_LINK, "user-example")[0]
    from tester.secrets import decode_secret

    fields = parse_qs(urlsplit(proxy.web_link()).query)
    assert fields["server"] == ["135.181.74.178"] and fields["port"] == ["443"]
    assert decode_secret(fields["secret"][0]).encoded == decode_secret("3XnnAQIAAQAH8AMDhuJMOt0").encoded
    assert proxy.web_link().startswith("https://t.me/proxy?")
    row = ProxyResult(proxy, [TestSample(True, total_latency_ms=100)]).to_dict()
    assert row["web_link"] == proxy.web_link()
    assert row["tg_link"].startswith("tg://proxy?")


def test_telegram_web_link_encodes_ipv6_and_reserved_secret_characters():
    proxy = Proxy("[2001:db8::1]", 443, "AQ+/=")
    parsed = urlsplit(proxy.web_link())
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "t.me", "/proxy")
    assert parse_qs(parsed.query) == {"server": ["2001:db8::1"], "port": ["443"], "secret": ["AQ+/="]}


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\n", encoding="utf-8")
    return AppConfig.load(path)


def test_telegram_scan_uses_mtproto_probe_and_exports_https_links(config, monkeypatch, capsys):
    import main
    from collector.collector import Collector
    from tester.runner import ParallelTester
    from webproxy.collector import WebCollector

    proxies = parse_text_blob(USER_LINK, "user-example") + [
        Proxy("other.example", 443, "00112233445566778899aabbccddeeff")
    ]
    expected_link = proxies[0].web_link()  # parser canonicalizes equivalent hex/base64 secrets
    monkeypatch.setattr(Collector, "collect", lambda self: proxies)
    monkeypatch.setattr(
        WebCollector, "collect", lambda self: pytest.fail("Telegram link scan used HTTP feeds")
    )
    monkeypatch.setattr(
        ParallelTester,
        "run",
        lambda self, selected: [
            ProxyResult(selected[0], [TestSample(True, total_latency_ms=100)]),
            ProxyResult(selected[1], [TestSample(False, failure_reason=FailureReason.TCP_TIMEOUT)]),
        ],
    )
    main.cmd_best10(config, mode="telegram", limit=2)
    output = capsys.readouterr().out
    folder = Path(config.output.folder) / "telegram"
    assert expected_link in output and "VERIFIED PROXIES: 1 / 10" in output
    assert "tg://proxy?" not in output
    snapshot = json.loads((folder / "snapshot.json").read_text())
    top = json.loads((folder / "top10.json").read_text())
    assert snapshot["mode"] == "telegram" and snapshot["rows"] == top
    assert top[0]["web_link"] == expected_link
    assert (folder / "telegram_links.txt").read_text().strip() == expected_link
    assert (folder / "proxy.txt").read_text().startswith(expected_link)
    assert expected_link in (folder / "best10_links.txt").read_text()
    csv_rows = list(csv.DictReader(StringIO((folder / "proxy.csv").read_text())))
    assert csv_rows[0]["web_link"] == expected_link
    assert "tg_link" not in csv_rows[0]
    assert not (Path(config.output.folder) / "web").exists()


def test_second_menu_option_means_telegram_web_links(config, monkeypatch, capsys):
    import builtins

    import main
    from collector.collector import Collector

    choices = iter(["2", "1", "6"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(choices))
    monkeypatch.setattr(Collector, "collect", lambda self: [])
    main.interactive_menu(config)
    output = capsys.readouterr().out
    assert "Telegram Web Link" in output and "HTTP / SOCKS" in output
    assert (Path(config.output.folder) / "telegram" / "snapshot.json").exists()


def test_direct_telegram_command_is_accepted(config, monkeypatch):
    import main
    from collector.collector import Collector

    monkeypatch.setattr(main, "load_config", lambda path=None: config)
    monkeypatch.setattr(Collector, "collect", lambda self: [])
    assert main.main(["best10", "--mode", "telegram", "--count", "ALL"]) == 0
    assert (Path(config.output.folder) / "telegram" / "snapshot.json").exists()


def test_saved_telegram_top_displays_a_valid_https_share_link(config, capsys):
    import main

    proxy = parse_text_blob(USER_LINK, "user-example")[0]
    result = ProxyResult(proxy, [TestSample(True, total_latency_ms=100)], score=0.8)
    folder = Path(config.output.folder) / "telegram"
    folder.mkdir(parents=True)
    (folder / "proxy.json").write_text(json.dumps([result.to_dict()]))
    main.cmd_top(config, 10, mode="telegram")
    assert proxy.web_link() in capsys.readouterr().out
