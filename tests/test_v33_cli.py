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
    monkeypatch.setattr(server, "run", lambda host, port, folder, **kwargs: calls.append(folder))
    assert main.cmd_dashboard(config, open_browser=False)
    assert calls == [str(Path(config.output.folder) / "networks" / "wifi")]


@pytest.mark.parametrize(
    "args", [["best10", "--network", "../mobile"], ["dashboard", "--resume"], ["collect", "--resume"]]
)
def test_invalid_network_or_resume_command_is_rejected_before_configuration(args, monkeypatch):
    monkeypatch.setattr(main, "load_config", lambda path=None: pytest.fail("invalid arguments loaded config"))
    with pytest.raises(SystemExit) as exc:
        main.main(args)
    assert exc.value.code == 2


def test_menu_can_select_network_resume_and_show_history(config, monkeypatch):
    choices = iter(["8", "mobile", "7", "2", "9", "2", "6"])
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
