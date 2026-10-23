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
