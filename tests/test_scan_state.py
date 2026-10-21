"""Persistence must preserve finished evidence without trusting saved rankings."""

import copy
import json
import time

import pytest

from config.loader import AppConfig
from tests.proxy_fixtures import qualified_result
from utils.models import FailureReason, Proxy, ProxyResult, TestSample
from utils.scan_state import (
    CheckpointStore,
    HistoryStore,
    deserialize_proxy,
    deserialize_result,
    scan_fingerprint,
    serialize_proxy,
    serialize_result,
    write_json_atomic,
)
from webproxy.models import WebProxy


def mtproxy():
    return Proxy("example.org", 443, "00" * 16, source="fixture")


def store(tmp_path, **kwargs):
    return CheckpointStore(
        tmp_path,
        mode=kwargs.pop("mode", "mtproto"),
        network=kwargs.pop("network", "wifi"),
        fingerprint=kwargs.pop("fingerprint", "a" * 64),
        max_age_seconds=kwargs.pop("max_age_seconds", 3600),
        **kwargs,
    )


@pytest.mark.parametrize("proxy", [mtproxy(), WebProxy("2001:db8::1", 8080, "socks5")])
def test_result_roundtrip_retains_evidence_but_not_stored_score(proxy):
    result = (
        qualified_result(proxy)
        if isinstance(proxy, Proxy)
        else ProxyResult(proxy, [TestSample(True, total_latency_ms=120)])
    )
    result.score = 0.999
    row = serialize_result(result)
    row.update(score=1, strict_eligible=True, successes=9999)
    loaded = deserialize_result(row)
    assert loaded.proxy.key() == result.proxy.key()
    assert loaded.samples == result.samples
    assert loaded.attempts == result.attempts
    assert loaded.successes == result.successes
    assert loaded.score == 0
    assert loaded.verification_profile == result.verification_profile


@pytest.mark.parametrize(
    "change",
    [
        {"server": "https://evil.invalid/a"},
        {"server": "999.1.1.1"},
        {"port": True},
        {"port": 65536},
        {"secret": "garbage"},
        {"kind": "other"},
    ],
)
def test_untrusted_candidate_fields_are_validated(change):
    row = serialize_proxy(mtproxy())
    row.update(change)
    with pytest.raises(ValueError):
        deserialize_proxy(row)


@pytest.mark.parametrize(
    "change",
    [
        {"success": "yes"},
        {"failure_reason": "invented"},
        {"timestamp": float("nan")},
        {"timestamp": time.time() + 7200},
        {"total_latency_ms": float("inf")},
        {"dc_id": True},
        {"ping_rtts_ms": [1, -1, 3]},
        {"verification_level": "magic"},
    ],
)
def test_untrusted_finished_samples_are_validated(change):
    row = serialize_result(qualified_result(mtproxy()))
    row["samples"][0].update(change)
    with pytest.raises(ValueError):
        deserialize_result(row)


def test_failure_sample_preserves_reason_and_missing_latency():
    result = ProxyResult(mtproxy(), [TestSample(False, failure_reason=FailureReason.TCP_TIMEOUT)])
    loaded = deserialize_result(serialize_result(result))
    assert loaded.samples == result.samples
    assert loaded.successes == 0


def test_checkpoint_roundtrip_and_completed_marker(tmp_path):
    checkpoint = store(tmp_path)
    state = {"run_id": "run-1", "results": [serialize_result(qualified_result(mtproxy()))]}
    checkpoint.save([mtproxy()], state)
    candidates, loaded = checkpoint.load()
    assert candidates[0].key() == mtproxy().key()
    assert loaded == state
    checkpoint.complete()
    assert checkpoint.path.exists()
    with pytest.raises(ValueError, match="complete"):
        checkpoint.load()


def test_checkpoint_guards_config_network_and_mode(tmp_path):
    checkpoint = store(tmp_path)
    checkpoint.save([mtproxy()], {})
    with pytest.raises(ValueError, match="configuration"):
        store(tmp_path, fingerprint="b" * 64).load()
    with pytest.raises(ValueError, match="missing"):
        store(tmp_path, network="mobile").load()
    with pytest.raises(ValueError, match="missing"):
        store(tmp_path, mode="telegram").load()
    row = json.loads(checkpoint.path.read_text())
    row["network"] = "mobile"
    checkpoint.path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="network"):
        checkpoint.load()


@pytest.mark.parametrize("field,delta", [("created_at", -7200), ("updated_at", 7200)])
def test_checkpoint_timestamps_are_guarded(tmp_path, field, delta):
    checkpoint = store(tmp_path)
    checkpoint.save([mtproxy()], {})
    row = json.loads(checkpoint.path.read_text())
    row[field] = time.time() + delta
    checkpoint.path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="stale|future|timestamp"):
        checkpoint.load()


def test_checkpoint_rejects_stale_sample_even_if_envelope_refreshed(tmp_path):
    checkpoint = store(tmp_path)
    row = serialize_result(qualified_result(mtproxy()))
    row["samples"][0]["timestamp"] = time.time() - 7200
    checkpoint.save([mtproxy()], {"results": [row]})
    with pytest.raises(ValueError, match="stale"):
        checkpoint.load()
    checkpoint.complete()
    assert json.loads(checkpoint.path.read_text())["complete"] is True


def test_atomic_replace_failure_keeps_original_checkpoint(tmp_path, monkeypatch):
    checkpoint = store(tmp_path)
    checkpoint.save([mtproxy()], {"counter": 1})

    def denied(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr("utils.scan_state.os.replace", denied)
    with pytest.raises(OSError, match="disk unavailable"):
        checkpoint.save([mtproxy()], {"counter": 2})
    assert checkpoint.load()[1] == {"counter": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_checkpoint_rejects_duplicate_keys_corruption_and_secrets(tmp_path):
    checkpoint = store(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        checkpoint.load()
    checkpoint.path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint.path.write_text('{"schema":1,"schema":2}')
    with pytest.raises(ValueError, match="duplicate"):
        checkpoint.load()
    with pytest.raises(ValueError, match="auth_key"):
        checkpoint.save([mtproxy()], {"nested": {"auth_key": "DO NOT SAVE"}})


def test_fingerprint_tracks_semantics_without_output_or_logger_paths():
    config = AppConfig.load()
    baseline = scan_fingerprint(config, "mtproto")
    changed = copy.deepcopy(config)
    changed.output.folder = "somewhere-else"
    changed.logging.level = "DEBUG"
    changed.dashboard.port += 1
    assert scan_fingerprint(changed, "mtproto") == baseline
    changed.testing.ping_count += 1
    assert scan_fingerprint(changed, "mtproto") != baseline
    changed = copy.deepcopy(config)
    changed.sources[0].url += "?token=new-secret"
    assert scan_fingerprint(changed, "mtproto") != baseline
    assert "secret" not in baseline
    assert scan_fingerprint(config, "web") != baseline
    changed = copy.deepcopy(config)
    changed.scan.resume = True
    changed.scan.history_enabled = False
    changed.scan.network = "mobile"
    assert scan_fingerprint(changed, "mtproto") == baseline
    changed.scan.finalist_retries += 1
    assert scan_fingerprint(changed, "mtproto") != baseline


def test_history_is_idempotent_and_separated_by_network(tmp_path):
    history = HistoryStore(tmp_path, "wifi", "mtproto")
    result = qualified_result(mtproxy())
    history.record("run-1", [result], {"interrupted": True})
    history.record("run-1", [result], {"interrupted": False})
    row = history.rows()[0]
    assert row["run_count"] == 1
    assert row["historical_attempts"] == 3
    assert row["historical_successes"] == 3
    assert row["attempts"] == 3
    assert row["qualified"] is True
    assert row["current_run_id"] == "run-1"
    assert row["last_tested"] == max(sample.timestamp for sample in result.samples)
    assert HistoryStore(tmp_path, "mobile", "mtproto").rows() == []
    history.record("run-2", [ProxyResult(mtproxy(), [TestSample(False)])], {})
    row = history.rows()[0]
    assert row["run_count"] == 2
    assert row["historical_attempts"] == 4
    assert row["historical_successes"] == 3
    assert row["qualified"] is False


def test_history_max_runs_and_invalid_file(tmp_path):
    history = HistoryStore(tmp_path, "wifi", "mtproto", max_runs=2)
    result = qualified_result(mtproxy())
    for index in range(3):
        history.record(f"run-{index}", [result], {})
    assert history.rows()[0]["run_count"] == 2
    history.path.write_text("{broken")
    with pytest.raises(ValueError, match="Invalid|corrupt"):
        history.rows()


def test_history_never_labels_untested_candidates_or_averages_as_p95(tmp_path):
    history = HistoryStore(tmp_path, "wifi", "web")
    proxy = WebProxy("example.org", 8080)
    history.record("run-0", [ProxyResult(proxy)], {})
    assert history.rows() == []
    result = ProxyResult(proxy, [TestSample(True, total_latency_ms=value) for value in [1, 2, 300]])
    history.record("run-1", [result], {})
    assert history.rows()[0]["p95_ms"] == 300


def test_bounds_and_json_number_safety(tmp_path, monkeypatch):
    import utils.scan_state as state_module

    checkpoint = store(tmp_path)
    monkeypatch.setattr(state_module, "MAX_BYTES", 128)
    checkpoint.path.write_bytes(b" " * 129)
    with pytest.raises(ValueError, match="safety limit"):
        checkpoint.load()
    with pytest.raises(ValueError, match="safety limit"):
        write_json_atomic(tmp_path / "progress.json", {"data": "x" * 129})
    with pytest.raises(ValueError, match="finite"):
        write_json_atomic(tmp_path / "progress.json", {"data": 10**999})


@pytest.mark.parametrize("network", ["../outside", "", "x" * 33, "wifi space", "نام"])
def test_store_network_slug_prevents_path_escape(tmp_path, network):
    with pytest.raises(ValueError, match="network"):
        store(tmp_path, network=network)
