"""Device selftest checks the installed runtime without Telegram or browser access."""

import json
import threading

import pytest

from config.loader import AppConfig
from utils import selftest


@pytest.fixture
def config(tmp_path):
    config = AppConfig.load()
    config.output.folder = str(tmp_path / "output")
    config.scan.network = "cellular"
    return config


def read_report(config):
    from scan import network_folder

    return json.loads((network_folder(config) / "selftest-report.json").read_text(encoding="utf-8"))


def test_real_offline_runtime_and_loopback_selftest(config, monkeypatch, capsys):
    import dashboard.server

    monkeypatch.setattr(
        dashboard.server, "open_dashboard", lambda *args: pytest.fail("Selftest launched a browser")
    )
    monkeypatch.setenv("TERMUX_VERSION", "fixture")
    assert selftest.run_selftest(config) == 0
    report = read_report(config)
    assert report["version"] == "3.4.0"
    assert report["all_passed"] is True
    assert report["termux"] is True
    assert report["scope"] == "offline runtime/loopback only"
    assert report["telegram_live_tested"] is False
    assert report["android_browser_intent_tested"] is False
    assert report["physical_android_workflows_tested"] is False
    assert {check["status"] for check in report["checks"]} == {"pass"}
    assert {check["name"] for check in report["checks"]} == {
        "dependency_yaml",
        "dependency_httpx",
        "dependency_Crypto",
        "dependency_idna",
        "scan_config",
        "crypto_runtime",
        "telegram_link_parser",
        "telegram_web_link_parser",
        "atomic_checkpoint",
        "dashboard_loopback",
        "dashboard_controls_loopback",
    }
    assert all(check["error_class"] is None for check in report["checks"])
    assert "selftest-report.json" in capsys.readouterr().out
    assert not any(thread.name.startswith("revmamad-selftest-") for thread in threading.enumerate())
    assert not any(thread.name == "revmamad-dashboard-scan" for thread in threading.enumerate())
    contents = json.dumps(report)
    assert "DEMO" not in contents  # Synthetic endpoint fixtures never enter the user report.
    assert "192.0.2.1" not in contents


def test_missing_dependency_has_actionable_guidance_and_no_private_exception_text(
    config, monkeypatch, capsys
):
    original = selftest.importlib.import_module

    def missing(name):
        if name == "httpx":
            raise ModuleNotFoundError("private https://token-secret.example")
        return original(name)

    monkeypatch.setattr(selftest.importlib, "import_module", missing)
    assert selftest.run_selftest(config) == 1
    report = read_report(config)
    assert report["all_passed"] is False
    failed = [check for check in report["checks"] if check["status"] == "fail"]
    assert failed == [{"name": "dependency_httpx", "status": "fail", "error_class": "ModuleNotFoundError"}]
    output = capsys.readouterr().out
    assert "requirements-lite.txt" in output
    assert "token-secret" not in output + json.dumps(report)


def test_failed_loopback_still_closes_server_and_thread(config, monkeypatch, capsys):
    import dashboard.server

    servers = []
    original = dashboard.server.create_server

    def create(*args, **kwargs):
        server = original(*args, **kwargs)
        servers.append(server)
        return server

    def broken(*args):
        raise OSError("private feed URL and token")

    monkeypatch.setattr(dashboard.server, "create_server", create)
    monkeypatch.setattr(selftest, "_http_get", broken)
    assert selftest.run_selftest(config) == 1
    report = read_report(config)
    assert next(check for check in report["checks"] if check["name"] == "dashboard_loopback") == {
        "name": "dashboard_loopback",
        "status": "fail",
        "error_class": "OSError",
    }
    assert len(servers) == 2 and all(server.socket.fileno() == -1 for server in servers)
    assert not any(thread.name.startswith("revmamad-selftest-") for thread in threading.enumerate())
    assert "private" not in capsys.readouterr().out + json.dumps(report)


def test_control_post_failure_stops_worker_closes_resources_and_redacts_report(config, monkeypatch, capsys):
    import dashboard.server

    servers, posts = [], []
    original_create = dashboard.server.create_server
    original_post = selftest._http_control

    def create(*args, **kwargs):
        server = original_create(*args, **kwargs)
        servers.append(server)
        return server

    def broken_stop(port, body, token, **kwargs):
        posts.append(body["action"])
        if body["action"] == "stop":
            raise OSError("private token and synthetic endpoint")
        return original_post(port, body, token, **kwargs)

    monkeypatch.setattr(dashboard.server, "create_server", create)
    monkeypatch.setattr(selftest, "_http_control", broken_stop)
    assert selftest.run_selftest(config) == 1
    report = read_report(config)
    assert posts == ["start", "start", "start", "stop"]
    assert next(check for check in report["checks"] if check["name"] == "dashboard_controls_loopback") == {
        "name": "dashboard_controls_loopback",
        "status": "fail",
        "error_class": "OSError",
    }
    assert len(servers) == 2 and all(server.socket.fileno() == -1 for server in servers)
    assert not any(thread.name.startswith("revmamad-selftest-") for thread in threading.enumerate())
    assert not any(thread.name == "revmamad-dashboard-scan" for thread in threading.enumerate())
    assert "private" not in capsys.readouterr().out + json.dumps(report)


def test_web_parser_diagnostic_rejects_a_wrong_capability_vector(monkeypatch):
    from telegram_webproxy import TelegramWebProxy

    monkeypatch.setattr(TelegramWebProxy, "bridge_capability", property(lambda self: "incorrect"))
    with pytest.raises(ValueError, match="known-answer"):
        selftest._check_web_parser()


def test_control_diagnostic_rejects_bypassed_csrf_guard(monkeypatch):
    original = selftest._http_control

    def bypassed(port, body, token, **kwargs):
        if token == "invalid-selftest-token":
            return 202, {"job": {"state": "running"}}
        return original(port, body, token, **kwargs)

    monkeypatch.setattr(selftest, "_http_control", bypassed)
    with pytest.raises(ValueError, match="invalid token or origin"):
        selftest._check_dashboard_controls()
    assert not any(thread.name.startswith("revmamad-selftest-") for thread in threading.enumerate())


def test_invalid_scan_config_is_failed_and_report_stays_in_output_root(config):
    from pathlib import Path

    config.scan.network = "../../outside"
    assert selftest.run_selftest(config) == 1
    report = json.loads((Path(config.output.folder) / "selftest-report.json").read_text(encoding="utf-8"))
    assert next(check for check in report["checks"] if check["name"] == "scan_config")["status"] == "fail"
    assert not report["all_passed"]


def test_report_write_failure_returns_failure_and_does_not_print_exception_content(
    config, monkeypatch, capsys
):
    import utils.scan_state

    def denied(*args):
        raise PermissionError("private token")

    monkeypatch.setattr(utils.scan_state, "write_json_atomic", denied)
    assert selftest.run_selftest(config) == 1
    output = capsys.readouterr().out
    assert "FAIL report_write (PermissionError)" in output
    assert "private" not in output
