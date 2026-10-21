"""Large web checkpoints remain transactional and validate every resumed row."""

import copy
import json
import sqlite3
import time

import pytest

from utils.large_checkpoint import SQLiteCheckpointStore
from utils.models import Proxy, ProxyResult, TestSample
from utils.scan_state import serialize_result
from webproxy.models import WebProxy


def store(tmp_path, **changes):
    settings = dict(mode="web", network="wifi", fingerprint="a" * 64, max_age_seconds=3600)
    settings.update(changes)
    return SQLiteCheckpointStore(tmp_path, **settings)


def state(results=(), **context):
    return {"engine": {"schema": 1, "stage": "testing", "results": list(results)}, "context": context}


def sample(proxy, latency=50, **changes):
    result = ProxyResult(proxy, [TestSample(True, total_latency_ms=latency, **changes)])
    return serialize_result(result)


def metadata(checkpoint, change=None):
    with sqlite3.connect(checkpoint.path) as connection:
        value = json.loads(connection.execute("SELECT payload FROM metadata WHERE id=1").fetchone()[0])
        if change:
            change(value)
            connection.execute("UPDATE metadata SET payload=? WHERE id=1", (json.dumps(value),))
        return value


def test_roundtrip_preserves_order_and_finished_samples_without_saved_rankings(tmp_path):
    proxies = [WebProxy("2001:db8::1", 8080, "https", "first"), WebProxy("192.0.2.1", 1080, "socks5")]
    row = sample(proxies[1])
    row.update(score=1, strict_eligible=True, successes=999)
    original = state([row], run_id="fixture")
    checkpoint = store(tmp_path)
    checkpoint.save(proxies, original)
    restored_proxies, restored_state = store(tmp_path).load()
    assert [proxy.key() for proxy in restored_proxies] == [proxy.key() for proxy in proxies]
    expected = copy.deepcopy(original)
    expected["engine"]["results"][0] = sample(proxies[1], timestamp=row["samples"][0]["timestamp"])
    assert restored_state == expected
    checkpoint.complete()
    assert metadata(checkpoint)["complete"] is True
    with pytest.raises(ValueError, match="complete"):
        checkpoint.load()


def test_actual_hundred_thousand_and_one_candidates_fit_beyond_json_limit(tmp_path):
    # Private synthetic addresses are only serialized; no network access occurs.
    proxies = [WebProxy(f"10.{i >> 16}.{(i >> 8) & 255}.{i & 255}", 8080) for i in range(100_001)]
    checkpoint = store(tmp_path)
    original = state([sample(proxies[-1])], demo=True)
    checkpoint.save(proxies, original)
    restored, loaded = store(tmp_path).load()
    assert len(restored) == 100_001
    assert restored[-1].key() == proxies[-1].key()
    assert loaded == original
    assert checkpoint.path.stat().st_size < 32 * 1024 * 1024


def test_incremental_save_writes_only_changed_result_and_candidate_rows(tmp_path):
    proxies = [WebProxy("192.0.2.1", 8080), WebProxy("192.0.2.2", 8080)]
    rows = [sample(proxy) for proxy in proxies]
    checkpoint = store(tmp_path)
    checkpoint.save(proxies, state(rows))
    with sqlite3.connect(checkpoint.path) as connection:
        connection.execute("CREATE TABLE audit (kind TEXT)")
        connection.execute(
            "CREATE TRIGGER changed_result AFTER UPDATE ON results BEGIN INSERT INTO audit VALUES('result'); END"
        )
        connection.execute(
            "CREATE TRIGGER changed_candidate AFTER UPDATE ON candidates BEGIN INSERT INTO audit VALUES('candidate'); END"
        )
    checkpoint.save(proxies, state(rows))
    with sqlite3.connect(checkpoint.path) as connection:
        assert connection.execute("SELECT * FROM audit").fetchall() == []
    rows[1]["samples"].append({**rows[1]["samples"][0], "total_latency_ms": 70})
    proxies[0].source = "new label"
    checkpoint.save(proxies, state(rows))
    with sqlite3.connect(checkpoint.path) as connection:
        assert sorted(connection.execute("SELECT * FROM audit").fetchall()) == [("candidate",), ("result",)]
    restored, loaded = store(tmp_path).load()
    assert restored[0].source == "new label"
    assert len(loaded["engine"]["results"][1]["samples"]) == 2


def test_same_sample_count_mutations_are_validated_and_reordering_stays_canonical(tmp_path):
    proxies = [WebProxy("192.0.2.1", 8080), WebProxy("192.0.2.2", 8080)]
    rows = [sample(proxy) for proxy in proxies]
    rows[0]["score"] = 1
    checkpoint = store(tmp_path)
    checkpoint.save(proxies, state(rows))
    checkpoint.save(proxies, state(list(reversed(rows))))
    loaded = checkpoint.load()[1]["engine"]["results"]
    assert [row["proxy"]["server"] for row in loaded] == ["192.0.2.2", "192.0.2.1"]
    assert "score" not in loaded[1]
    invalid = copy.deepcopy(rows)
    invalid[0]["samples"][0]["total_latency_ms"] = -1
    with pytest.raises(ValueError, match="finite|sample"):
        checkpoint.save(proxies, state(invalid))
    assert checkpoint.load()[1]["engine"]["results"] == loaded


def test_failed_fresh_replacement_rolls_back_deletes_and_preserves_old_run(tmp_path):
    proxy = WebProxy("192.0.2.1", 8080)
    checkpoint = store(tmp_path)
    checkpoint.save([proxy], state([sample(proxy)], run_id="original"))
    replacement = WebProxy("192.0.2.2", 1080, "socks4")
    invalid = sample(replacement)
    invalid["samples"][0]["success"] = "not bool"
    with pytest.raises(ValueError, match="success"):
        store(tmp_path).save([replacement], state([invalid], run_id="replacement"))
    restored, loaded = store(tmp_path).load()
    assert restored[0].key() == proxy.key()
    assert loaded["context"]["run_id"] == "original"
    fresh = store(tmp_path)
    fresh.save([replacement], state([sample(replacement)], run_id="replacement"))
    assert fresh.load()[0][0].key() == replacement.key()


def test_connection_open_operational_failure_is_safe_oserror(tmp_path, monkeypatch):
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("private path and feed token")

    monkeypatch.setattr("utils.large_checkpoint.sqlite3.connect", unavailable)
    with pytest.raises(OSError, match="storage is unavailable") as error:
        store(tmp_path).save([], state())
    assert "private" not in str(error.value)


def test_operational_failure_rolls_back_closes_and_maps_to_safe_oserror(tmp_path, monkeypatch):
    proxy = WebProxy("192.0.2.1", 8080)
    checkpoint = store(tmp_path)
    original_state = state([sample(proxy)], run_id="original")
    checkpoint.save([proxy], original_state)
    connect = sqlite3.connect
    connections = []

    class DiskFull(sqlite3.Connection):
        rolled_back = closed = False

        def execute(self, statement, *args, **kwargs):
            result = super().execute(statement, *args, **kwargs)
            if statement == "DELETE FROM results":
                raise sqlite3.OperationalError("disk full private token")
            return result

        def rollback(self):
            self.rolled_back = True
            return super().rollback()

        def close(self):
            self.closed = True
            return super().close()

    def broken_connect(*args, **kwargs):
        connection = connect(*args, **kwargs, factory=DiskFull)
        connections.append(connection)
        return connection

    with monkeypatch.context() as patch:
        patch.setattr("utils.large_checkpoint.sqlite3.connect", broken_connect)
        with pytest.raises(OSError, match="storage is unavailable") as error:
            store(tmp_path).save([proxy], state())
        assert "private" not in str(error.value)
    assert connections[0].rolled_back and connections[0].closed
    assert store(tmp_path).load()[1] == original_state


@pytest.mark.parametrize(
    "change,match",
    [
        ("stale", "stale"),
        ("future", "future"),
        ("config", "configuration"),
        ("network", "network"),
        ("schema", "schema"),
    ],
)
def test_envelope_identity_and_time_guards(tmp_path, change, match):
    proxy = WebProxy("192.0.2.1", 8080)
    checkpoint = store(tmp_path)
    checkpoint.save([proxy], state())
    changes = {
        "stale": lambda row: row.update(created_at=time.time() - 7200),
        "future": lambda row: row.update(updated_at=time.time() + 7200),
        "config": lambda row: row.update(fingerprint="b" * 64),
        "network": lambda row: row.update(network="other"),
        "schema": lambda row: row.update(schema=999),
    }
    metadata(checkpoint, changes[change])
    with pytest.raises(ValueError, match=match):
        checkpoint.load()


def test_stale_sample_cannot_resume_but_long_active_run_can_complete(tmp_path):
    proxy = WebProxy("192.0.2.1", 8080)
    checkpoint = store(tmp_path)
    checkpoint.save([proxy], state([sample(proxy, timestamp=time.time() - 7200)]))
    with pytest.raises(ValueError, match="stale"):
        checkpoint.load()
    checkpoint.complete()
    assert metadata(checkpoint)["complete"] is True


@pytest.mark.parametrize(
    "invalid",
    [
        {"nested": {"auth_key": "private"}},
        {"url": "https://feed.example/?token=private"},
        {"https://feed.example/?token=private": "value"},
        {"value": float("nan")},
    ],
)
def test_private_fields_urls_and_nonfinite_context_are_rejected(tmp_path, invalid):
    with pytest.raises(ValueError):
        store(tmp_path).save([], state(**invalid))


def test_results_must_belong_to_candidates_without_duplicates(tmp_path):
    proxy, other = WebProxy("192.0.2.1", 80), WebProxy("192.0.2.2", 80)
    checkpoint = store(tmp_path)
    with pytest.raises(ValueError, match="duplicate candidates"):
        checkpoint.save([proxy, proxy], state())
    with pytest.raises(ValueError, match="selected candidate"):
        checkpoint.save([proxy], state([sample(other)]))
    with pytest.raises(ValueError, match="duplicate results"):
        checkpoint.save([proxy], state([sample(proxy), sample(proxy)]))
    with pytest.raises(ValueError, match="web proxies"):
        checkpoint.save([Proxy("192.0.2.1", 443, "00" * 16)], state())


def test_candidate_identity_changes_need_fresh_scan_and_resumed_store_can_save(tmp_path):
    proxy = WebProxy("192.0.2.1", 8080)
    original = store(tmp_path)
    original.save([proxy], state([sample(proxy)]))
    resumed = store(tmp_path)
    candidates, saved = resumed.load()
    saved["engine"]["results"][0]["samples"].append(sample(proxy)["samples"][0])
    resumed.save(candidates, saved)
    assert len(resumed.load()[1]["engine"]["results"][0]["samples"]) == 2
    with pytest.raises(ValueError, match="identity changed"):
        resumed.save([WebProxy("192.0.2.2", 8080)], saved)


def test_result_private_material_and_oversized_candidate_source_are_rejected(tmp_path):
    proxy = WebProxy("192.0.2.1", 8080)
    invalid = sample(proxy)
    invalid["auth_key"] = "do not persist"
    with pytest.raises(ValueError, match="private field"):
        store(tmp_path).save([proxy], state([invalid]))
    proxy.source = "x" * 2049
    with pytest.raises(ValueError, match="proxy source"):
        store(tmp_path).save([proxy], state())


def test_corrupt_rows_counts_and_sqlite_file_are_rejected(tmp_path):
    checkpoint = store(tmp_path)
    with pytest.raises(ValueError, match="missing"):
        checkpoint.load()
    assert not checkpoint.path.exists()
    proxy = WebProxy("192.0.2.1", 8080)
    checkpoint.save([proxy], state([sample(proxy)]))
    with sqlite3.connect(checkpoint.path) as connection:
        connection.execute("UPDATE results SET sample_count=99")
    with pytest.raises(ValueError, match="result identity"):
        checkpoint.load()
    with sqlite3.connect(checkpoint.path) as connection:
        connection.execute("DELETE FROM results")
    with pytest.raises(ValueError, match="row counts"):
        checkpoint.load()
    with pytest.raises(ValueError, match="row counts"):
        checkpoint.complete()
    checkpoint.path.write_bytes(b"not SQLite")
    with pytest.raises(ValueError, match="corrupt"):
        checkpoint.load()


def test_caps_and_constructor_reject_invalid_backends_and_networks(tmp_path, monkeypatch):
    import utils.large_checkpoint as module

    with pytest.raises(ValueError, match="web scans"):
        store(tmp_path, mode="mtproto")
    with pytest.raises(ValueError, match="network"):
        store(tmp_path, network="../../outside")
    monkeypatch.setattr(module, "MAX_CANDIDATES", 1)
    with pytest.raises(ValueError, match="maximum"):
        store(tmp_path).save([WebProxy("192.0.2.1", 80), WebProxy("192.0.2.2", 80)], state())
    monkeypatch.setattr(module, "MAX_ROW_BYTES", 1024)
    with pytest.raises(ValueError, match="safety limit"):
        store(tmp_path).save([], state(data="x" * 2048))
