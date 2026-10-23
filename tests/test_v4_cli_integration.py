"""User configuration and saved scan evidence cross actual CLI boundaries."""

import json
from dataclasses import asdict

import pytest

import main
from config.loader import AppConfig
from scan import mode_folder
from tests.test_v4_media import qualified
from utils.settings import save_settings


def isolated(tmp_path):
    config = AppConfig.load()
    config.output.folder = str(tmp_path / "output")
    config.logging.log_dir = str(tmp_path / "logs")
    config._user_data_dir = tmp_path / "personal"
    config.scan.network = "mobile"
    return config


def test_personal_settings_roundtrip_and_explicit_data_dir_cli(tmp_path, monkeypatch):
    config = isolated(tmp_path)
    path = save_settings(config)
    loaded = AppConfig.load(path)
    assert loaded.scan.network == "mobile"
    assert loaded.output.folder == config.output.folder
    assert loaded.sources == config.sources
    config.scan.network = "wifi"
    save_settings(config)
    assert AppConfig.load(path.with_suffix(".backup.yaml")).scan.network == "mobile"
    observed = []
    monkeypatch.setattr(main, "cmd_sources", lambda value, args: observed.append(value))
    assert main.main(["sources", "--data-dir", str(config._user_data_dir)]) == 0
    assert observed[0].scan.network == "wifi"
    assert observed[0]._user_data_dir == config._user_data_dir
    assert not observed[0]._external_config


def test_media_selection_uses_real_observation_serialization_and_current_ttl(tmp_path):
    config = isolated(tmp_path)
    folder = mode_folder(config, "mtproto")
    folder.mkdir(parents=True)
    result = qualified(age=120)
    row = dict(result.to_dict(), samples=[asdict(item) for item in result.samples])
    payload = {"version": 1, "mode": "mtproto", "summary": {"network": "mobile"}, "rows": [row]}
    path = folder / "observations.json"
    path.write_text(json.dumps(payload))
    assert len(main._media_candidates(config, "mtproto")) == 1
    config.output.proof_max_age_minutes = 1
    assert main._media_candidates(config, "mtproto") == []
    payload["summary"]["network"] = "wifi"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="network"):
        main._media_candidates(config, "mtproto")


def test_noninteractive_media_never_prompts_or_creates_session(tmp_path, monkeypatch):
    pytest.importorskip("telethon")
    from media_test import runner

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(runner.getpass, "getpass", lambda *args: pytest.fail("Prompted without TTY"))
    with pytest.raises(RuntimeError, match="interactive terminal"):
        runner.run_media_test([qualified()], network_profile="wifi", message_link="https://t.me/example/1")
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("prefix", [[], ["--config", "settings.yaml"]])
def test_recovery_command_bypasses_broken_runtime_with_global_options(tmp_path, monkeypatch, prefix):
    from utils import updater

    config = isolated(tmp_path)
    monkeypatch.setattr(main, "load_config", lambda path=None: config)
    monkeypatch.setattr(main, "setup_logger", lambda **kwargs: None)
    monkeypatch.setattr(updater, "_read", lambda path: pytest.fail("Recovery used broken candidate runtime"))
    calls = []
    monkeypatch.setattr(updater, "update", lambda root, **kwargs: calls.append(kwargs) or {})
    assert main.main([*prefix, "update", "--rollback"], managed_runtime=True) == 0
    assert calls[0]["rollback"] is True


def test_launcher_allows_recovery_while_pending_update_exists(tmp_path, monkeypatch):
    import launcher

    state = tmp_path / ".revmamad" / "updates"
    state.mkdir(parents=True)
    (state / "last.json").write_text('{"phase":"prepared"}')
    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "launcher.py"))
    monkeypatch.setattr(launcher.sys, "argv", ["launcher.py", "update", "--rollback"])
    calls = []
    monkeypatch.setattr(launcher.subprocess, "call", lambda argv, **kwargs: calls.append(argv) or 0)
    assert launcher.main() == 0
    assert calls[0][2:] == ["update", "--rollback"]


@pytest.mark.parametrize("argv", [["doctor"], ["calibration", "evaluate", "--ledger", "unused.json"]])
def test_pending_update_blocks_regular_commands_with_readable_error(tmp_path, monkeypatch, capsys, argv):
    from utils import updater

    monkeypatch.setattr(updater, "_read", lambda path: {"phase": "prepared"})
    monkeypatch.setattr(main, "load_config", lambda path=None: pytest.fail("Loaded before runtime check"))
    assert main.main(argv, managed_runtime=True) == 1
    output = capsys.readouterr()
    assert "update --rollback" in output.out + output.err
    assert "Traceback" not in output.out + output.err


@pytest.mark.parametrize("argv", [["doctor"], ["calibration", "evaluate", "--ledger", "unused.json"]])
def test_commands_handoff_to_validated_runtime_before_loading_dependencies(tmp_path, monkeypatch, argv):
    from utils import updater

    executable = tmp_path / "validated-python"
    monkeypatch.setattr(updater, "_read", lambda path: None)
    monkeypatch.setattr(updater, "runtime_python", lambda *args: executable)
    calls = []

    def execute(path, arguments):
        calls.append((path, arguments))
        raise SystemExit(91)

    monkeypatch.setattr(main.os, "execv", execute)
    with pytest.raises(SystemExit) as exit_info:
        main.main(argv, managed_runtime=True)
    assert exit_info.value.code == 91
    assert calls == [(str(executable), [str(executable), str(main.PROJECT_ROOT / "main.py"), *argv])]


@pytest.mark.parametrize("mode", ["mtproto", "telegram", "web", "webproxy"])
@pytest.mark.parametrize("age", [1900, 120])
def test_top_omits_expired_saved_results_under_current_ttl(tmp_path, capsys, mode, age):
    from telegram_webproxy.models import TelegramWebProxy
    from tests.test_v4_engine import qualified as proof
    from webproxy.models import WebProxy

    config = isolated(tmp_path)
    if age == 120:
        config.output.proof_max_age_minutes = 1
    result = proof()
    if mode == "web":
        result.proxy = WebProxy("expired.example", 8080)
    elif mode == "webproxy":
        result.proxy = TelegramWebProxy("expired.example", "a" * 32)
    else:
        result.proxy.server = "expired.example"
    folder = mode_folder(config, mode)
    folder.mkdir(parents=True)
    # Export while fresh, then simulate reading those exact status flags later.
    row = result.to_dict()
    for key in ("oldest_observed_at", "latest_observed_at", "evidence_expires_at"):
        row[key] -= age
    (folder / "proxy.json").write_text(json.dumps([row]))
    main.cmd_top(config, 10, mode=mode)
    output = capsys.readouterr().out
    assert "expired.example" not in output
    assert "fresh" in output.lower()


def test_terminal_best10_rechecks_http_proxy_freshness(capsys):
    from utils.models import ProxyResult, TestSample
    from webproxy.models import WebProxy

    result = ProxyResult(
        WebProxy("expired.example", 8080), [TestSample(True, total_latency_ms=20, timestamp=1)]
    )
    result.score = 0.9
    main.print_best10([result], "output", mode="web")
    output = capsys.readouterr().out
    assert "expired.example" not in output
    assert "VERIFIED PROXIES: 0 / 10" in output
