import pytest

from config.loader import AppConfig, ScanConfig


def test_scan_defaults_enable_bounded_stages_and_separate_network_history():
    config = ScanConfig().validate()
    assert config.strategy == "staged" and config.prefilter
    assert (config.finalists, config.finalist_retries, config.finalist_workers) == (20, 5, 4)
    assert config.network == "default" and config.checkpoint_max_age_minutes == 30


@pytest.mark.parametrize(
    "name,value",
    [
        ("strategy", "unknown"),
        ("prefilter", "true"),
        ("resume", 1),
        ("network", "../other"),
        ("network", ""),
        ("network", "a" * 33),
        ("network", "CON"),
        ("finalists", True),
        ("finalists", 101),
        ("finalist_retries", 0),
        ("finalist_ping_count", 11),
        ("finalist_workers", 17),
        ("source_failure_threshold", 1),
        ("finalist_ping_interval_seconds", float("nan")),
        ("finalist_ping_interval_seconds", 0),
        ("checkpoint_max_age_minutes", 0),
        ("source_cooldown_minutes", float("inf")),
    ],
)
def test_scan_configuration_rejects_unsafe_or_unbounded_values(name, value):
    with pytest.raises(ValueError):
        ScanConfig(**{name: value}).validate()


def test_resume_requires_a_checkpoint():
    with pytest.raises(ValueError, match="checkpoint"):
        ScanConfig(resume=True, checkpoint_enabled=False).validate()


def test_network_names_have_consistent_case_across_platforms():
    assert ScanConfig(network="WiFi").validate().network == "wifi"


def test_external_config_accepts_scan_settings_and_keeps_its_paths(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nscan:\n  network: mobile\n  strategy: single\n")
    config = AppConfig.load(path)
    assert config.scan.network == "mobile" and config.scan.strategy == "single"
    assert config.output.folder == str(tmp_path / "output")
