import builtins
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest

import main
from config.loader import AppConfig
from utils.models import FailureReason, Proxy, ProxyResult, TestSample


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\n", encoding="utf-8")
    return AppConfig.load(path)


@pytest.fixture
def cli_config(config, monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda path=None: config)
    monkeypatch.setattr(main, "setup_logger", lambda **kwargs: logging.getLogger("test-cli"))
    return config


@pytest.mark.parametrize("flag, expected", [("--help", "--no-browser"), ("--version", "REVMAMAD 3.1.0")])
def test_help_and_version_work_without_any_installed_packages(tmp_path, flag, expected):
    result = subprocess.run(
        [sys.executable, "-S", str(main.PROJECT_ROOT / "main.py"), flag],
        cwd=tmp_path,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout


@pytest.mark.parametrize(
    "args", [["top", "oops"], ["top", "0"], ["top", "-2"], ["collect", "2"], ["unknown"]]
)
def test_bad_cli_arguments_are_rejected_before_loading_config(args, monkeypatch, capsys):
    monkeypatch.setattr(main, "load_config", lambda path=None: pytest.fail("invalid args loaded config"))
    with pytest.raises(SystemExit) as exc:
        main.main(args)
    assert exc.value.code == 2
    assert "error:" in capsys.readouterr().err


def test_menu_exercises_every_action_and_returns_after_dashboard(cli_config, monkeypatch, capsys):
    choices = iter(["invalid", "1", "1", "2", "2", "12", "3", "2", "5", "4", "5", "3", "6"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(choices))
    called = []
    monkeypatch.setattr(main, "cmd_best10", lambda cfg, **kw: called.append(("test", kw)))
    monkeypatch.setattr(main, "cmd_collect", lambda cfg, **kw: called.append(("collect", kw)))
    monkeypatch.setattr(
        main, "cmd_dashboard", lambda cfg, **kwargs: called.append(("dashboard", kwargs["open_browser"]))
    )
    assert main.main(["menu", "--no-browser"]) == 0
    assert called == [
        ("test", {"mode": "mtproto", "limit": None}),
        ("test", {"mode": "telegram", "limit": 12}),
        ("test", {"mode": "web", "limit": 5}),
        ("dashboard", False),
        ("collect", {"mode": "web"}),
    ]
    output = capsys.readouterr().out
    assert "REVMAMAD v3.1.0" in output and "Invalid choice" in output and "Bye!" in output


def test_menu_survives_action_failure_and_interrupt(cli_config, monkeypatch, capsys):
    choices = iter(["1", "1", "2", "1", "6"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(choices))

    def fail(cfg, *, mode, limit):
        if mode == "mtproto":
            raise OSError("offline source unavailable")
        raise KeyboardInterrupt

    monkeypatch.setattr(main, "cmd_best10", fail)
    assert main.main([]) == 0
    output = capsys.readouterr().out
    assert "offline source unavailable" in output and "Returning to the menu" in output and "Bye!" in output


@pytest.mark.parametrize("exc, expected", [(EOFError, "No keyboard input"), (KeyboardInterrupt, "Bye!")])
def test_menu_no_input_and_control_c_are_clean(cli_config, monkeypatch, capsys, exc, expected):
    def no_input(prompt):
        raise exc

    monkeypatch.setattr(builtins, "input", no_input)
    assert main.main([]) == 0
    assert expected in capsys.readouterr().out


@pytest.mark.parametrize("command", ["best10", "collect", "benchmark", "export", "scheduler", "json", "csv"])
def test_cli_dispatches_all_direct_commands(cli_config, monkeypatch, command):
    calls = []
    monkeypatch.setattr(main, "cmd_" + command, lambda cfg: calls.append(cfg))
    assert main.main([command]) == 0
    assert calls == [cli_config]


def test_run_and_top_and_dashboard_command_dispatch(cli_config, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "run_pipeline", lambda cfg, export: calls.append(("run", export)) or [])
    monkeypatch.setattr(main, "cmd_top", lambda cfg, n: calls.append(("top", n)))
    monkeypatch.setattr(
        main, "cmd_dashboard", lambda cfg, **kwargs: calls.append(("dashboard", kwargs["open_browser"]))
    )
    assert main.main(["run"]) == 0
    assert main.main(["top", "20"]) == 0
    assert main.main(["top"]) == 0
    assert main.main(["dashboard", "--no-browser"]) == 0
    assert calls == [("run", True), ("top", 20), ("top", 10), ("dashboard", False)]


def test_cli_operational_errors_return_nonzero(cli_config, monkeypatch, capsys):
    def fail(cfg):
        raise ValueError("broken export")

    monkeypatch.setattr(main, "cmd_collect", fail)
    assert main.main(["collect"]) == 1
    assert "broken export" in capsys.readouterr().out


def test_missing_runtime_dependency_explains_lite_install(cli_config, monkeypatch, capsys):
    def fail(cfg):
        raise ModuleNotFoundError("httpx")

    monkeypatch.setattr(main, "cmd_collect", fail)
    assert main.main(["collect"]) == 1
    assert "requirements-lite.txt" in capsys.readouterr().out


def test_banner_fits_32_column_termux(monkeypatch, capsys):
    monkeypatch.setattr(main, "_width", lambda: 32)
    main.print_banner()
    lines = capsys.readouterr().out.splitlines()
    assert max(map(len, lines)) <= 32
    assert any("#" in line for line in lines)
    assert "REVMAMAD v3.1.0" in lines


def make_result(server, *, success, failure=FailureReason.NONE, latency=100):
    proxy = Proxy(server, 443, "11223344556677889900aabbccddeeff")
    return ProxyResult(
        proxy,
        samples=[TestSample(success, total_latency_ms=latency if success else None, failure_reason=failure)],
    )


def test_pipeline_real_scoring_and_export_with_mocked_network(config, monkeypatch, capsys):
    from collector.collector import Collector
    from tester.runner import ParallelTester

    results = [
        make_result("1.1.1.1", success=True),
        make_result("2.2.2.2", success=False, failure=FailureReason.TCP_TIMEOUT),
        make_result("3.3.3.3", success=False, failure=FailureReason.UNSUPPORTED_TRANSPORT),
    ]
    monkeypatch.setattr(Collector, "collect", lambda self: [result.proxy for result in results])
    monkeypatch.setattr(ParallelTester, "run", lambda self, proxies: results)
    main.cmd_best10(config)
    output = capsys.readouterr().out
    assert "VERIFIED PROXIES: 1 / 10" in output
    assert "guaranteed count" in output
    assert "unsupported_transport=1" in output and "tcp_timeout=1" in output
    folder = Path(config.output.folder) / "mtproto"
    data = json.loads((folder / "proxy.json").read_text(encoding="utf-8"))
    assert [proxy["server"] for proxy in data] == ["1.1.1.1"]
    assert (folder / "best10_links.txt").exists() and (folder / "proxy.csv").exists()


def test_best10_display_matches_saved_links_with_export_limits(config, monkeypatch, capsys):
    from collector.collector import Collector
    from tester.runner import ParallelTester

    results = [
        make_result("fast.example", success=True, latency=10),
        make_result("slow.example", success=True, latency=200),
    ]
    config.output.max_latency_ms = 50
    monkeypatch.setattr(Collector, "collect", lambda self: [result.proxy for result in results])
    monkeypatch.setattr(ParallelTester, "run", lambda self, proxies: results)
    main.cmd_best10(config)
    output = capsys.readouterr().out
    saved = (Path(config.output.folder) / "mtproto" / "best10_links.txt").read_text(encoding="utf-8")
    assert "fast.example" in output and "fast.example" in saved
    assert "slow.example" not in output and "slow.example" not in saved
    assert "VERIFIED PROXIES: 1 / 10" in output


def test_dashboard_receives_custom_output_and_browser_choice(config, monkeypatch):
    from dashboard import server

    calls = []
    monkeypatch.setattr(server, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert main.cmd_dashboard(config, open_browser=False) is True
    assert calls == [
        ((config.dashboard.host, config.dashboard.port, config.output.folder), {"open_browser": False})
    ]


def test_empty_scan_replaces_stale_output(config, monkeypatch):
    from collector.collector import Collector
    from tester.runner import ParallelTester

    folder = Path(config.output.folder) / "mtproto"
    folder.mkdir(parents=True)
    (folder / "proxy.json").write_text('[{"server": "stale"}]', encoding="utf-8")
    monkeypatch.setattr(Collector, "collect", lambda self: [])
    monkeypatch.setattr(ParallelTester, "run", lambda *args: pytest.fail("tested an empty candidate list"))
    assert main.run_pipeline(config) == []
    assert json.loads((folder / "proxy.json").read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("command, filename", [("json", "proxy.json"), ("csv", "proxy.csv")])
def test_json_csv_commands_generate_files(config, monkeypatch, capsys, command, filename):
    from collector.collector import Collector

    monkeypatch.setattr(Collector, "collect", lambda self: [])
    getattr(main, "cmd_" + command)(config)
    path = Path(config.output.folder) / "mtproto" / filename
    assert path.exists()
    assert str(path) in capsys.readouterr().out


def test_top_handles_missing_empty_and_corrupt_exports(config, capsys):
    main.cmd_top(config, 10)
    assert "No exported data" in capsys.readouterr().out
    folder = Path(config.output.folder)
    folder.mkdir()
    path = folder / "proxy.json"
    path.write_text("[]", encoding="utf-8")
    main.cmd_top(config, 10)
    assert "no verified proxies" in capsys.readouterr().out
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="Unreadable export"):
        main.cmd_top(config, 10)
    path.write_text('[{"server": "test", "port": 443, "score": "bad"}]', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid score"):
        main.cmd_top(config, 10)


def test_top_limits_saved_output(config, capsys):
    folder = Path(config.output.folder)
    folder.mkdir()
    path = folder / "proxy.json"
    path.write_text(
        json.dumps(
            [
                {"server": f"proxy{i}.example", "port": 443, "score": 0.9, "avg_latency_ms": 30}
                for i in range(3)
            ]
        ),
        encoding="utf-8",
    )
    main.cmd_top(config, 2)
    output = capsys.readouterr().out
    assert "proxy0.example" in output and "proxy1.example" in output and "proxy2.example" not in output
