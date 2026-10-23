"""Old evidence is discarded without losing the retained candidate pool."""

import asyncio
import copy
import json
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone

import pytest

from config.loader import AppConfig
from telegram_webproxy.models import TelegramWebProxy
from tester.staged import StagedTester
from tests.proxy_fixtures import qualified_result
from tests.test_staged import candidates, config, install_probes, passing_sample
from utils.large_checkpoint import SQLiteCheckpointStore
from utils.models import Proxy, ProxyResult, TestSample
from utils.resume_refresh import refresh_saved_evidence
from utils.scan_state import (
    CheckpointStore,
    HistoryStore,
    deserialize_proxy,
    deserialize_result,
    scan_fingerprint,
    serialize_proxy,
    serialize_result,
)
from webproxy.models import WebProxy


def stamp(seconds=0):
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).isoformat()


def envelope(results=(), **changes):
    return {
        "context": {"run_id": "same-run", "started_at": stamp(), **changes},
        "engine": {"schema": 1, "stage": "testing", "results": list(results)},
    }


@pytest.fixture(params=["json", "sqlite"])
def backend(request, tmp_path):
    cls = CheckpointStore if request.param == "json" else SQLiteCheckpointStore
    proxy = (
        Proxy("proxy.example", 443, "a" * 32) if request.param == "json" else WebProxy("proxy.example", 80)
    )

    def make():
        return cls(
            tmp_path,
            mode="mtproto" if request.param == "json" else "web",
            network="wifi",
            fingerprint="a" * 64,
            max_age_seconds=1800,
        )

    def metadata(checkpoint, change=None):
        if request.param == "json":
            row = json.loads(checkpoint.path.read_text())
            if change:
                change(row)
                checkpoint.path.write_text(json.dumps(row))
        else:
            with sqlite3.connect(checkpoint.path) as connection:
                row = json.loads(connection.execute("SELECT payload FROM metadata WHERE id=1").fetchone()[0])
                if change:
                    change(row)
                    connection.execute("UPDATE metadata SET payload=? WHERE id=1", (json.dumps(row),))
        return row

    return make, proxy, metadata


def test_refresh_load_keeps_old_samples_visible_for_explicit_discard(backend):
    make, proxy, metadata = backend
    result = qualified_result(proxy) if isinstance(proxy, Proxy) else ProxyResult(proxy, [TestSample(True)])
    result.samples[0].timestamp = time.time() - 7200
    checkpoint = make()
    original = envelope([serialize_result(result)], started_at=stamp(-7200))
    checkpoint.save([proxy], original)
    with pytest.raises(ValueError, match="stale"):
        checkpoint.load()
    restored, state = checkpoint.load_for_refresh()
    assert [row.key() for row in restored] == [proxy.key()]
    assert state == original


def test_refresh_pool_retention_does_not_renew_after_resave(backend):
    make, proxy, metadata = backend
    checkpoint = make()
    checkpoint.save([proxy], envelope())
    origin = time.time() - 6 * 86400
    metadata(checkpoint, lambda row: row.update(created_at=origin))
    resumed = make()
    candidates, state = resumed.load_for_refresh()
    resumed.save(candidates, state)
    assert metadata(resumed)["created_at"] == origin
    metadata(resumed, lambda row: row.update(created_at=time.time() - 8 * 86400))
    with pytest.raises(ValueError, match="retention|stale"):
        make().load_for_refresh()


@pytest.mark.parametrize(
    "change,match",
    [
        (lambda row: row.update(complete=True), "complete"),
        (lambda row: row.update(fingerprint="b" * 64), "configuration"),
        (lambda row: row.update(network="mobile"), "network"),
        (lambda row: row.update(updated_at=time.time() + 7200), "future"),
        (lambda row: row.update(schema=999), "schema"),
    ],
)
def test_refresh_preserves_completion_configuration_future_and_identity_guards(backend, change, match):
    make, proxy, metadata = backend
    checkpoint = make()
    checkpoint.save([proxy], envelope())
    metadata(checkpoint, change)
    with pytest.raises(ValueError, match=match):
        make().load_for_refresh()


def test_refresh_helper_discards_all_samples_for_one_stale_candidate():
    cfg = AppConfig.load()
    proxies = [Proxy("stale.example", 443, "a" * 32), Proxy("fresh.example", 443, "a" * 32)]
    stale, fresh = [qualified_result(proxy) for proxy in proxies]
    stale.samples[0].timestamp = time.time() - 7200
    original = envelope([serialize_result(stale), serialize_result(fresh)], started_at=stamp(-7200))
    before = copy.deepcopy(original)
    changed, stats = refresh_saved_evidence(cfg, proxies, original)
    assert original == before
    assert stats["stale_candidates"] == 1 and stats["stale_samples"] == 3
    assert stats["retained_candidates"] == 1 and stats["retained_samples"] == 3
    assert [row["proxy"]["server"] for row in changed["engine"]["results"]] == ["fresh.example"]
    assert changed["context"]["run_id"] == "same-run"
    assert changed["context"]["candidate_pool_created_at"] == before["context"]["started_at"]
    assert changed["engine"]["stage"] == "testing"


def test_refresh_fresh_envelope_keeps_frozen_engine_and_old_pool_origin():
    cfg = AppConfig.load()
    proxy = Proxy("fresh.example", 443, "a" * 32)
    state = envelope([serialize_result(qualified_result(proxy))], candidate_pool_created_at=stamp(-3600))
    changed, stats = refresh_saved_evidence(cfg, [proxy], state)
    assert changed == state and not stats["refreshed"]
    assert changed is not state


@pytest.mark.parametrize("change", ["future", "nonmember", "duplicates", "old_pool", "profile", "bad_stage"])
def test_refresh_never_erases_corrupt_evidence_into_valid_state(change):
    cfg = AppConfig.load()
    proxy = Proxy("proxy.example", 443, "a" * 32)
    row = serialize_result(qualified_result(proxy))
    state = envelope([row], started_at=stamp(-7200))
    if change == "future":
        row["samples"][0]["timestamp"] = time.time() + 7200
    elif change == "nonmember":
        row["proxy"]["server"] = "other.example"
    elif change == "duplicates":
        state["engine"]["results"].append(copy.deepcopy(row))
    elif change == "old_pool":
        state["context"]["candidate_pool_created_at"] = stamp(-8 * 86400)
    elif change == "profile":
        row["verification_profile"] = "transport"
    elif change == "bad_stage":
        state["engine"]["stage"] = "unknown"
    with pytest.raises(ValueError):
        refresh_saved_evidence(cfg, [proxy], state)


@pytest.mark.asyncio
async def test_staged_refresh_preserves_fresh_partial_finalist_and_rechecks_only_expired_candidates(
    monkeypatch,
):
    cfg = config()
    install_probes(monkeypatch)
    engine = StagedTester(cfg)
    await engine.run_async(candidates(3))
    saved = copy.deepcopy(engine.state)
    saved["stage"] = "finalists"
    first = candidates()[0].key()
    saved["finalist_attempts"][first] = 4
    saved["results"][0]["samples"].pop()
    for row in saved["results"][1:]:
        for sample in row["samples"]:
            sample["timestamp"] -= 7200
    state = {"context": {"run_id": "same-run", "started_at": stamp(-7200)}, "engine": saved}
    changed, stats = refresh_saved_evidence(cfg, candidates(3), state)
    assert stats["stale_candidates"] == 2 and stats["retained_samples"] == 7
    assert changed["engine"]["completed_finalists"] == {first: 4}
    assert changed["engine"]["prefilter"] == {first: {"passed": True, "reason": "none"}}
    assert changed["engine"]["coverage_limited"] and not changed["engine"]["budget_round_finished"]
    calls = Counter()

    async def probe(proxy, **kwargs):
        calls[proxy.key()] += 1
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=probe)
    resumed = StagedTester(cfg, initial_state=changed["engine"])
    results = await resumed.run_async(candidates(3))
    assert calls[first] == 1 and results[0].attempts == 8
    assert results[0].strict_eligible


@pytest.mark.asyncio
async def test_refreshed_partial_finalist_finishes_even_if_new_candidates_rank_higher(monkeypatch):
    cfg = config()
    cfg.scan.finalists = 1
    install_probes(monkeypatch)
    engine = StagedTester(cfg)
    await engine.run_async(candidates(2))
    saved = copy.deepcopy(engine.state)
    first = candidates()[0].key()
    saved.update(stage="finalists", finalist_attempts={first: 4})
    saved["results"][0]["samples"].pop()
    for sample in saved["results"][0]["samples"]:
        sample["ping_rtts_ms"] = [1000.0] * len(sample["ping_rtts_ms"])
    for sample in saved["results"][1]["samples"]:
        sample["timestamp"] -= 7200
    state = {"context": {"run_id": "same-run", "started_at": stamp(-7200)}, "engine": saved}
    changed, _ = refresh_saved_evidence(cfg, candidates(2), state)
    calls = Counter()

    async def probe(proxy, **kwargs):
        calls[proxy.key()] += 1
        latency = 1000 if proxy.key() == first else 10
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"], latency)

    install_probes(monkeypatch, strict=probe)
    results = await StagedTester(cfg, initial_state=changed["engine"]).run_async(candidates(2))
    assert calls[first] == 1 and results[0].attempts == 8
    assert results[1].attempts == 3 and not results[1].strict_eligible


@pytest.mark.asyncio
async def test_completed_checkpoint_cannot_hide_an_unfinished_archived_finalist(monkeypatch):
    cfg = config()
    install_probes(monkeypatch)
    engine = StagedTester(cfg)
    await engine.run_async(candidates(1))
    saved = copy.deepcopy(engine.state)
    first = candidates()[0].key()
    saved.update(finalist_keys=[], finalist_attempts={}, completed_finalists={first: 4})
    saved["results"][0]["samples"].pop()
    with pytest.raises(ValueError, match="before all finalist"):
        await StagedTester(cfg, initial_state=saved).run_async(candidates(1))


@pytest.mark.parametrize("server", ["relay.example", "relay.example/base/path"])
def test_webproxy_persistence_history_and_strict_profile_roundtrip(tmp_path, server):
    proxy = TelegramWebProxy(server, "a" * 32)
    result = qualified_result(proxy)
    restored = deserialize_result(serialize_result(result))
    assert isinstance(restored.proxy, TelegramWebProxy) and restored.proxy.server == server
    assert restored.strict_eligible
    checkpoint = CheckpointStore(
        tmp_path, mode="webproxy", network="wifi", fingerprint="a" * 64, max_age_seconds=1800
    )
    checkpoint.save([proxy], envelope([serialize_result(result)]))
    assert checkpoint.load()[0][0].key() == proxy.key()
    history = HistoryStore(tmp_path, "wifi", "webproxy")
    history.record("same-run", [result], {})
    row = history.rows()[0]
    assert row["server"] == server and row["kind"] == row["protocol"] == "webproxy"
    assert row["qualified"] and row["p95_ms"] == result.telegram_p95_ms
    assert "secret" not in json.dumps(row)


@pytest.mark.parametrize(
    "change",
    [{"protocol": "mtproto"}, {"kind": "mtproto"}, {"port": 80}, {"server": "https://relay.example"}],
)
def test_webproxy_candidate_kind_protocol_port_and_address_cannot_mix(change):
    row = serialize_proxy(TelegramWebProxy("relay.example/base", "a" * 32))
    row.update(change)
    with pytest.raises(ValueError):
        deserialize_proxy(row)


@pytest.mark.parametrize("mode", ["mtproto", "telegram", "web"])
def test_webproxy_evidence_cannot_enter_another_mode_store(tmp_path, mode):
    proxy = TelegramWebProxy("relay.example", "a" * 32)
    with pytest.raises(ValueError, match="kind"):
        CheckpointStore(tmp_path, mode=mode, network="wifi", fingerprint="a" * 64, max_age_seconds=1800).save(
            [proxy], {}
        )
    with pytest.raises(ValueError, match="kind"):
        HistoryStore(tmp_path, "wifi", mode).record("same-run", [qualified_result(proxy)], {})


@pytest.mark.parametrize(
    "change", [{"protocol": "mtproto"}, {"protocol": []}, {"kind": "mtproto"}, {"port": 80}]
)
def test_webproxy_history_rejects_mixed_protocols_even_when_endpoint_is_valid(tmp_path, change):
    proxy = TelegramWebProxy("relay.example/base", "a" * 32)
    history = HistoryStore(tmp_path, "wifi", "webproxy")
    history.record("same-run", [qualified_result(proxy)], {})
    payload = json.loads(history.path.read_text())
    payload["runs"][0]["rows"][0].update(change)
    history.path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        history.rows()


def test_webproxy_fingerprint_tracks_its_sources_and_excludes_operational_controls():
    cfg = AppConfig.load()
    baseline = scan_fingerprint(cfg, "webproxy")
    changed = copy.deepcopy(cfg)
    changed.scan.adaptive_workers = False
    changed.scan.time_budget_seconds = 600
    changed.scan.finalist_reserve_fraction = 0.8
    changed.scan.refresh_stale_on_resume = False
    assert scan_fingerprint(changed, "webproxy") == baseline
    changed.sources[0].url += "?different=mtproto"
    assert scan_fingerprint(changed, "webproxy") == baseline
    changed.webproxy_sources[0].url += "?different=webproxy"
    assert scan_fingerprint(changed, "webproxy") != baseline


@pytest.mark.asyncio
async def test_staged_webproxy_resume_keeps_finished_proof_and_continues_frozen_finalist(monkeypatch):
    cfg = config()
    proxy = TelegramWebProxy("relay.example/base", "a" * 32)
    calls = 0

    async def first_probe(proxy, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise asyncio.CancelledError
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=first_probe)
    engine = StagedTester(cfg)
    with pytest.raises(asyncio.CancelledError):
        await engine.run_async([proxy])
    assert engine.partial_results[0].attempts == 4

    async def resumed_probe(proxy, **kwargs):
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=resumed_probe)
    resumed = StagedTester(cfg, initial_state=engine.state)
    result = (await resumed.run_async([proxy]))[0]
    assert result.attempts == 8 and result.strict_eligible
