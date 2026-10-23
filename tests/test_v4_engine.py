"""Behavioral regression checks for cross-process ownership and current proof."""

import asyncio
import concurrent.futures
import copy
import json
import os
import subprocess
import sys
import time

import pytest

from benchmark.scorer import score_result
from config.loader import AppConfig, OutputConfig, ScoringConfig
from exporter.exporter import Exporter, eligible_results
from tester.staged import StagedTester
from utils.models import Proxy, ProxyResult, TestSample
from utils.process_lock import OutputRootBusy, OutputRootLock, output_root_busy
from utils.scan_state import deserialize_result, serialize_result


def qualified(*, stamp=None, latency=30):
    stamp = time.time() if stamp is None else stamp
    result = ProxyResult(Proxy("proxy.example", 443, "a" * 32), verification_profile="telegram_session")
    result.samples = [
        TestSample(
            True,
            total_latency_ms=100,
            timestamp=stamp,
            verification_level="telegram_session",
            dc_id=dc,
            ping_rtts_ms=[latency] * 3,
        )
        for dc in (2, 4, 2)
    ]
    result.score = score_result(result, ScoringConfig())
    return result


def test_writer_ownership_across_processes_and_abnormal_exit(tmp_path):
    assert not output_root_busy(tmp_path)
    assert not (tmp_path / ".state").exists()
    code = "from utils.process_lock import OutputRootLock; import sys; import time;\nwith OutputRootLock(sys.argv[1]):\n print('owned',flush=True); time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "owned"
        assert output_root_busy(tmp_path)
        with pytest.raises(OutputRootBusy):
            with OutputRootLock(tmp_path):
                pytest.fail("Concurrent writer acquired root")
    finally:
        child.kill()
        child.wait(timeout=10)
        child.stdout.close()
    with OutputRootLock(tmp_path):
        assert (tmp_path / ".state" / "writer.lock").exists()
    # Ownership metadata is never deleted; kernel state, not PID age, owns it.
    assert (tmp_path / ".state" / "writer.lock").exists()
    assert not output_root_busy(tmp_path)


def test_writer_same_thread_nesting_does_not_release_early(tmp_path):
    def attempt():
        with OutputRootLock(tmp_path):
            return True

    with OutputRootLock(tmp_path):
        with OutputRootLock(tmp_path):
            pass
        with concurrent.futures.ThreadPoolExecutor() as pool:
            with pytest.raises(OutputRootBusy):
                pool.submit(attempt).result()
    assert attempt()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fork semantics")
def test_fork_does_not_inherit_reentrant_ownership(tmp_path):
    with OutputRootLock(tmp_path):
        pid = os.fork()
        if pid == 0:
            try:
                with OutputRootLock(tmp_path):
                    os._exit(2)
            except OutputRootBusy:
                os._exit(0)
        assert os.waitpid(pid, 0)[1] == 0


def test_fresh_sample_does_not_rehabilitate_old_proof(tmp_path):
    result = qualified()
    result.samples[0].timestamp -= 1801
    assert not result.strict_eligible and result.quality_eligible
    assert result.freshness_status == "expired"
    assert eligible_results([result], OutputConfig()) == []
    exporter = Exporter(OutputConfig(folder=str(tmp_path)))
    exporter.export_txt([result])
    exporter.export_telegram_links([result])
    exporter.export_top_n([result], 10)
    assert (tmp_path / "proxy.txt").read_text() == ""
    assert (tmp_path / "telegram_links.txt").read_text() == ""
    assert json.loads((tmp_path / "top10.json").read_text()) == []


def test_export_rechecks_freshness_after_scoring(monkeypatch, tmp_path):
    result = qualified(stamp=time.time() - 1790)
    assert eligible_results([result], OutputConfig()) == [result]
    now = time.time()
    monkeypatch.setattr("utils.models.time.time", lambda: now + 20)
    Exporter(OutputConfig(folder=str(tmp_path))).export_all([result])
    assert json.loads((tmp_path / "proxy.json").read_text()) == []


def test_metrics_keep_within_session_noise_separate_from_drift():
    result = qualified()
    result.samples[0].ping_rtts_ms = [10, 10, 10]
    result.samples[1].ping_rtts_ms = [500, 500, 500]
    result.samples[2].ping_rtts_ms = [10, 10, 10]
    assert result.within_session_jitter_ms == 0
    assert result.interconnection_drift_ms > 200
    old = score_result(result, ScoringConfig())
    new = score_result(result, ScoringConfig(policy="v2-experimental"))
    assert new < old and result.ranking_policy == "v2-experimental"


def test_old_schema_still_readable_without_claiming_renewed_proof():
    result = qualified(stamp=time.time() - 3600)
    wire = serialize_result(result)
    for field in ("ranking_policy", "proof_max_age_seconds", "provisional"):
        wire.pop(field)
    restored = deserialize_result(wire)
    assert restored.ranking_policy == "v1" and restored.freshness_status == "expired"
    assert not restored.strict_eligible


def test_v34_default_checkpoint_fingerprint_survives_metadata_only_additions():
    from dataclasses import asdict
    from types import SimpleNamespace

    from utils.scan_state import scan_fingerprint

    cfg = AppConfig.load()
    old = SimpleNamespace(**vars(cfg))
    old.scoring, old.output = asdict(cfg.scoring), asdict(cfg.output)
    old.scoring.pop("policy")
    old.output.pop("proof_max_age_minutes")
    assert scan_fingerprint(cfg, "mtproto") == scan_fingerprint(old, "mtproto")
    cfg.scoring.policy = "v2-experimental"
    assert scan_fingerprint(cfg, "mtproto") != scan_fingerprint(old, "mtproto")


def test_memberships_roundtrip_and_private_url_rejection():
    result = qualified()
    result.proxy.source_memberships = ["a" * 64, "b" * 64]
    assert (
        deserialize_result(serialize_result(result)).proxy.source_memberships
        == result.proxy.source_memberships
    )
    result.proxy.source_memberships.append("https://private-feed.example/?password=test")
    with pytest.raises(ValueError, match="memberships"):
        serialize_result(result)


@pytest.mark.asyncio
async def test_v2_finalists_are_interleaved_and_resume_seed_persists(monkeypatch):
    cfg = AppConfig.load()
    cfg.scoring.policy = "v2-experimental"
    cfg.scan.prefilter = False
    cfg.scan.finalists = cfg.scan.finalist_workers = 3
    cfg.scan.finalist_reconnect_delay_seconds = 0
    proxies = [Proxy(f"proxy{i}.example", 443, "a" * 32) for i in range(3)]
    calls, snapshots = [], []

    async def probe(proxy, **kwargs):
        if kwargs["ping_count"] > cfg.testing.ping_count:
            calls.append(proxy.server)
            await asyncio.sleep(0.001 if proxy.server == "proxy0.example" else 0.005)
        sample = qualified().samples[0]
        sample.dc_id = kwargs["dc_id"]
        sample.ping_rtts_ms = [30] * kwargs["ping_count"]
        return sample

    monkeypatch.setattr("tester.staged.test_proxy_strict", probe)
    engine = StagedTester(cfg, on_checkpoint=lambda state: snapshots.append(copy.deepcopy(state)))
    await engine.run_async(proxies)
    for offset in range(0, len(calls), 3):
        assert set(calls[offset : offset + 3]) == {p.server for p in proxies}
    partial = next(
        row
        for row in snapshots
        if row["stage"] == "finalists" and sum(row["finalist_attempts"].values()) == 2
    )
    restored = StagedTester(cfg, initial_state=partial)
    calls.clear()
    await restored.run_async(proxies)
    assert restored.state["round_seed"] == partial["round_seed"]
    assert len(calls) == 13
    assert all(result.attempts == 8 for result in restored.partial_results)
