"""Native CLI integration: isolated local inputs, no public proxy probes."""

import json
import subprocess
import sys

import pytest

import main
from main import PROJECT_ROOT


@pytest.fixture
def native_config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\nwebproxy_sources: []\n", encoding="utf-8")
    return path


def invoke(config, *args, stdin=None):
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "main.py"), "--config", str(config), *args],
        cwd=config.parent,
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )


@pytest.mark.parametrize("mode", ["mtproto", "telegram", "web", "webproxy"])
def test_native_empty_modes_keep_network_time_budget_and_export_scope(native_config, mode):
    result = invoke(
        native_config,
        "best10",
        "--mode",
        mode,
        "--network",
        "Phone-QA",
        "--minutes",
        "0.02",
        "--fixed-workers",
        "--count",
        "ALL",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "VERIFIED PROXIES: 0 / 10" in result.stdout
    folder = native_config.parent / "output" / "networks" / "phone-qa" / mode
    assert json.loads((folder / "proxy.json").read_text(encoding="utf-8")) == []
    snapshot = json.loads((folder / "snapshot.json").read_text(encoding="utf-8"))
    summary = snapshot["summary"]
    assert summary["network"] == "phone-qa" and snapshot["mode"] == mode
    assert summary["time_budget_seconds"] == 1.2
    assert summary["selected"] == summary["tested"] == 0
    links = (folder / "best10_links.txt").read_text(encoding="utf-8")
    assert "Top 0 Verified" in links and "tg://" not in links and "https://" not in links
    assert {path.name for path in folder.parent.iterdir()} == {mode}


def test_native_web_import_deduplicates_share_schemes_and_preserves_back_and_invalid_input(native_config):
    native_config.write_text(
        "sources: []\nweb_sources: []\nwebproxy_sources:\n"
        "  - name: local-web\n    type: http_txt\n    url: file://manual-web.txt\n",
        encoding="utf-8",
    )
    link = "tg://webproxy?server=proxy.example.com&secret=000102030405060708090a0b0c0d0e0f"
    for value in (link, link.replace("tg://webproxy?", "https://t.me/webproxy?")):
        result = invoke(native_config, "add-webproxy", "--link", value)
        assert result.returncode == 0, result.stdout + result.stderr
    saved = native_config.parent / "manual-web.txt"
    assert [line for line in saved.read_text(encoding="utf-8").splitlines() if line] == [link]
    before = saved.read_bytes()
    invalid = invoke(native_config, "add-webproxy", "--link", link.replace("webproxy?", "proxy?"))
    assert invalid.returncode == 1 and "Use a valid tg://webproxy" in " ".join(invalid.stdout.split())
    back = invoke(native_config, "menu", "--no-browser", stdin="12\n0\n6\n")
    assert back.returncode == 0 and "Bye!" in back.stdout
    assert saved.read_bytes() == before
    collected = invoke(native_config, "collect", "--mode", "webproxy")
    assert collected.returncode == 0 and "1 unique proxy candidates" in collected.stdout
    # Collection/import never opens a relay connection or claims qualification.
    assert not (native_config.parent / "output" / "webproxy" / "proxy.json").exists()


@pytest.mark.parametrize("minutes", ["0", "nan", "inf", "-1", "1441"])
def test_native_invalid_time_budget_is_rejected_before_output(native_config, minutes):
    result = invoke(native_config, "best10", "--mode", "webproxy", "--minutes", minutes)
    assert result.returncode == 2
    assert "Minutes must be a finite number" in result.stderr
    assert not (native_config.parent / "output").exists()


def test_native_menu_web_and_custom_time_back_routes_do_not_start_a_scan(native_config):
    result = invoke(
        native_config,
        "menu",
        "--no-browser",
        stdin="13\n0.02\n11\n0\n12\n0\n5\n4\n9\n4\n7\n0\n10\n0\n6\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "13) Testing time: 0.02 minutes" in result.stdout
    assert "[WEBPROXY] Collected 0 unique proxy candidates" in result.stdout
    assert "mode=webproxy" in result.stdout and "Bye!" in result.stdout
    assert "VERIFIED PROXIES:" not in result.stdout
    assert not (native_config.parent / "output" / "webproxy" / "proxy.json").exists()


def test_new_cli_flags_reach_the_web_scan_dispatch_with_custom_loaded_config(native_config, monkeypatch):
    called = []

    def capture(config, *, mode, limit):
        called.append(
            (
                mode,
                limit,
                config.scan.network,
                config.scan.time_budget_seconds,
                config.scan.adaptive_workers,
                config._path,
            )
        )

    monkeypatch.setattr(main, "cmd_best10", capture)
    assert (
        main.main(
            [
                "best10",
                "--config",
                str(native_config),
                "--mode",
                "webproxy",
                "--count",
                "17",
                "--network",
                "Mobile",
                "--minutes",
                "2.5",
                "--fixed-workers",
            ]
        )
        == 0
    )
    assert called == [("webproxy", 17, "mobile", 150.0, False, native_config)]
