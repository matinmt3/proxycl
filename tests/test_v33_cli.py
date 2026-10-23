import builtins
from pathlib import Path

import pytest

import main
from config.loader import AppConfig


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\n")
    cfg = AppConfig.load(path)
    monkeypatch.setattr(main, "load_config", lambda path=None: cfg)
    monkeypatch.setattr(main, "setup_logger", lambda **kwargs: None)
    return cfg


def test_cli_flags_select_network_and_resume_without_changing_other_profiles(config, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "cmd_best10", lambda cfg: calls.append((cfg.scan.network, cfg.scan.resume)))
    assert main.main(["best10", "--network", "mobile", "--resume"]) == 0
    assert calls == [("mobile", True)]
    assert config.output.folder.endswith("output")


def test_named_network_dashboard_uses_only_its_snapshots(config, monkeypatch):
    from dashboard import server

    config.scan.network = "wifi"
    calls = []
    monkeypatch.setattr(
        server, "run", lambda host, port, folder, **kwargs: calls.append((folder, kwargs.get("controller")))
    )
    assert main.cmd_dashboard(config, open_browser=False)
    assert calls[0][0] == str(Path(config.output.folder))
    assert calls[0][1].status()["network"] == "wifi"


def test_run_shows_saved_links_in_selected_network_folder(config, monkeypatch, capsys):
    monkeypatch.setattr(main, "run_pipeline", lambda cfg, export: [])
    assert main.main(["run", "--network", "mobile"]) == 0
    expected = Path(config.output.folder) / "networks" / "mobile" / "mtproto" / "best10_links.txt"
    assert str(expected) in capsys.readouterr().out


@pytest.mark.parametrize(
    "args", [["best10", "--network", "../mobile"], ["dashboard", "--resume"], ["collect", "--resume"]]
)
def test_invalid_network_or_resume_command_is_rejected_before_configuration(args, monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda path=None: pytest.fail("invalid arguments loaded config"))
    with pytest.raises(SystemExit) as exc:
        main.main(args)
    assert exc.value.code == 2


def test_menu_can_select_network_resume_and_show_history(config, monkeypatch):
    choices = iter(["8", "mobile", "7", "2", "1", "9", "2", "6"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(choices))
    calls = []
    monkeypatch.setattr(
        main,
        "cmd_best10",
        lambda cfg, **kwargs: calls.append(("resume", cfg.scan.resume, cfg.scan.network, kwargs)),
    )
    monkeypatch.setattr(
        main,
        "cmd_history",
        lambda cfg, **kwargs: calls.append(("history", cfg.scan.resume, cfg.scan.network, kwargs)),
    )
    assert main.main(["menu"]) == 0
    assert calls == [
        ("resume", True, "mobile", {"mode": "telegram", "limit": None}),
        ("history", False, "mobile", {"mode": "telegram"}),
    ]


@pytest.mark.parametrize("resume_choice, command", [("1", "cmd_best10"), ("2", "cmd_recheck")])
@pytest.mark.parametrize("previous", [False, True])
def test_menu_routes_resume_choice_and_restores_previous_setting(
    config, monkeypatch, resume_choice, command, previous
):
    config.scan.resume = previous
    config.scan.network = "mobile"
    choices = iter(["7", "3", resume_choice, "6"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(choices))
    calls = []
    monkeypatch.setattr(
        main,
        command,
        lambda cfg, **kwargs: calls.append((cfg.scan.resume, cfg.scan.network, kwargs)),
    )
    other = "cmd_recheck" if command == "cmd_best10" else "cmd_best10"
    monkeypatch.setattr(main, other, lambda *args, **kwargs: pytest.fail("wrong resume command"))
    assert main.main(["menu"]) == 0
    assert calls == [(True, "mobile", {"mode": "web", "limit": None})]
    assert config.scan.resume is previous


@pytest.mark.parametrize("exception", [OSError("checkpoint unavailable"), KeyboardInterrupt()])
def test_menu_recheck_resume_restores_setting_after_failure(config, monkeypatch, exception):
    choices = iter(["7", "1", "2", "6"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(choices))

    def fail(cfg, **kwargs):
        assert cfg.scan.resume is True
        raise exception

    monkeypatch.setattr(main, "cmd_recheck", fail)
    assert main.main(["menu"]) == 0
    assert config.scan.resume is False


@pytest.mark.parametrize("resume_choice", ["0", "invalid"])
def test_menu_cancel_or_invalid_resume_does_not_run_a_scan(config, monkeypatch, resume_choice):
    choices = iter(["7", "1", resume_choice, "6"])
    monkeypatch.setattr(builtins, "input", lambda prompt: next(choices))
    for command in ("cmd_best10", "cmd_recheck"):
        monkeypatch.setattr(main, command, lambda *args, **kwargs: pytest.fail("cancelled resume ran"))
    assert main.main(["menu"]) == 0
    assert config.scan.resume is False
