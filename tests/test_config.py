from pathlib import Path

import pytest

from config.loader import AppConfig


def write_config(tmp_path, content):
    path = tmp_path / "settings.yaml"
    path.write_text(content, encoding="utf-8")
    return path


def test_default_config_does_not_depend_on_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = AppConfig.load()
    project = Path(__file__).resolve().parents[1]
    assert config._path == project / "config" / "config.yaml"
    assert Path(config.output.folder) == project / "output"
    assert Path(config.logging.log_dir) == project / "logs"
    local = next(source for source in config.sources if source.url.startswith("file://"))
    assert local.url == "file://" + str(project / "config" / "manual_proxies.txt")


def test_custom_config_paths_resolve_relative_to_config(tmp_path, monkeypatch):
    path = write_config(
        tmp_path,
        "sources:\n  - name: manual\n    type: http_txt\n    url: file://manual%20proxies.txt\noutput:\n  folder: results\nlogging:\n  log_dir: journal\n",
    )
    monkeypatch.chdir(tmp_path.parent)
    config = AppConfig.load(path)
    assert config.output.folder == str(tmp_path / "results")
    assert config.logging.log_dir == str(tmp_path / "journal")
    assert config.sources[0].url == "file://" + str(tmp_path / "manual proxies.txt")


@pytest.mark.parametrize(
    "content, message",
    [
        ("[one, two]", "YAML mapping"),
        ("testing: []", "testing must be a YAML mapping"),
        ("sources: {}", "sources must be a YAML list"),
        ("sources: [null]", "must be a YAML mapping"),
        ("sources: [{name: manual}]", "Invalid sources"),
        (
            "sources: [{name: manual, type: http_txt, url: test, enabled: 'false'}]",
            "enabled must be true or false",
        ),
        ("testing: {workers: 0}", "greater than zero"),
        ("testing: {workers: 1.5}", "integer"),
        ("testing: {retries: -1}", "greater than zero"),
        ("testing: {timeout_seconds: .inf}", "finite number"),
        ("testing: {connect_timeout_seconds: false}", "finite number"),
        ("testing: {typo: 1}", "Invalid testing"),
        ("scoring: {latency_weight: -0.2}", "cannot be negative"),
        ("scoring: {max_latency_ms: 0}", "greater than zero"),
        (
            "scoring: {latency_weight: 0, success_rate_weight: 0, stability_weight: 0, timeout_weight: 0}",
            "At least one scoring",
        ),
        ("output: {min_score: 2}", "between 0 and 1"),
        ("output: {top_sizes: [0]}", "greater than zero"),
        ("output: {folder: ''}", "non-empty string"),
        ("scheduler: {mode: invalid}", "scheduler.mode"),
        ("scheduler: {interval_minutes: 0}", "greater than zero"),
        ("dashboard: {port: 65536}", "between 1 and 65535"),
        ("logging: {level: invalid}", "logging.level"),
        ("typo: 1", "Unknown config section"),
        ("testing: [", "Invalid YAML"),
    ],
)
def test_invalid_config_has_actionable_error(tmp_path, content, message):
    with pytest.raises(ValueError, match=message):
        AppConfig.load(write_config(tmp_path, content))


def test_null_sections_use_defaults_and_top_sizes_deduplicate(tmp_path):
    config = AppConfig.load(write_config(tmp_path, "testing: null\noutput: {top_sizes: [10, 10, 50]}\n"))
    assert config.testing.workers == 200
    assert config.output.top_sizes == [10, 50]


def test_missing_config_names_the_file(tmp_path):
    with pytest.raises(FileNotFoundError, match="missing.yaml"):
        AppConfig.load(tmp_path / "missing.yaml")
