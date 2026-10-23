"""Regressions found during the independent final engine audit."""

import asyncio
import json
import time

import pytest

from config.loader import AppConfig, OutputConfig
from exporter.exporter import Exporter
from tests.test_v4_engine import qualified
from utils.evidence import saved_evidence_status
from utils.models import ProxyResult, TestSample
from webproxy.exporter import WebExporter
from webproxy.models import WebProxy


def test_every_direct_web_export_rejects_expired_proof(tmp_path):
    result = ProxyResult(
        WebProxy("proxy.example", 8080), [TestSample(True, total_latency_ms=50, timestamp=time.time() - 1801)]
    )
    result.score = 0.9
    exporter = WebExporter(OutputConfig(folder=str(tmp_path)))
    exporter.export_txt([result])
    exporter.export_proxy_links([result])
    exporter.export_csv([result])
    assert (tmp_path / "proxy.txt").read_text() == ""
    assert (tmp_path / "proxy_links.txt").read_text() == ""
    assert "proxy.example" not in (tmp_path / "proxy.csv").read_text()


def test_direct_top_n_uses_same_order_as_readable_export(tmp_path):
    fast, slow = qualified(latency=20), qualified(latency=400)
    fast.proxy.server, slow.proxy.server = "fast.example", "slow.example"
    exporter = Exporter(OutputConfig(folder=str(tmp_path)))
    exporter.export_top_n([slow, fast], 1)
    exporter.export_best_n_readable([slow, fast], 1)
    assert json.loads((tmp_path / "top1.json").read_text())[0]["server"] == "fast.example"
    assert "fast.example" in (tmp_path / "best10_links.txt").read_text()


def proof_row():
    return {
        "oldest_observed_at": 1000,
        "latest_observed_at": 1100,
        "proof_max_age_seconds": 1800,
        "evidence_expires_at": 2800,
        "report_schema": 2,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"latest_observed_at": 999},
        {"latest_observed_at": 1201},
        {"proof_max_age_seconds": True},
        {"proof_max_age_seconds": 59},
        {"proof_max_age_seconds": 86401},
        {"oldest_observed_at": float("nan")},
        {"latest_observed_at": float("inf")},
        {"evidence_expires_at": 2801},
        {"oldest_observed_at": 10**500},
        {"report_schema": 99},
        {"latest_observed_at": None},
    ],
)
def test_saved_proof_rejects_malformed_or_forged_times(change):
    assert saved_evidence_status({**proof_row(), **change}, now=1140) == "invalid"


def test_saved_proof_checks_expiry_boundary_and_current_stricter_ttl():
    assert saved_evidence_status({}, now=1140) == "missing"
    assert saved_evidence_status({"evidence_expires_at": 2800}, now=1140) == "invalid"
    assert saved_evidence_status(proof_row(), now=2799) == "fresh"
    assert saved_evidence_status(proof_row(), now=2800) == "expired"
    assert saved_evidence_status(proof_row(), now=1140, max_age_seconds=120) == "expired"
    assert saved_evidence_status(proof_row(), now=1140, max_age_seconds=float("nan")) == "invalid"


def test_model_rejects_unbounded_timestamp_without_crashing():
    result = qualified(stamp=10**500)
    assert not result.evidence_fresh()
    assert result.freshness_status == "invalid"


def test_resume_retests_at_exact_expiry_instead_of_preserving_stale_proof(monkeypatch):
    from datetime import datetime, timezone

    from utils.resume_refresh import refresh_saved_evidence
    from utils.scan_state import serialize_result

    now = time.time()
    result = qualified(stamp=now - 1800)
    state = {
        "context": {
            "run_id": "boundary",
            "started_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        },
        "engine": {"schema": 1, "stage": "testing", "results": [serialize_result(result)]},
    }
    monkeypatch.setattr("utils.resume_refresh.time.time", lambda: now)
    changed, stats = refresh_saved_evidence(AppConfig.load(), [result.proxy], state)
    assert stats["stale_candidates"] == 1
    assert stats["stale_samples"] == 3
    assert changed["engine"]["results"] == []


@pytest.mark.parametrize("web", [False, True])
def test_readable_export_marks_provisional_and_expiry(tmp_path, web):
    result = (
        ProxyResult(WebProxy("proxy.example", 80), [TestSample(True, total_latency_ms=50)])
        if web
        else qualified()
    )
    result.score = 0.9
    result.provisional = True
    exporter = (WebExporter if web else Exporter)(OutputConfig(folder=str(tmp_path)))
    content = exporter.export_best_n_readable([result]).read_text()
    assert "PROVISIONAL results" in content and "Result: PROVISIONAL" in content
    assert "Expires (UTC epoch):" in content


@pytest.mark.asyncio
async def test_stopped_staged_rows_and_checkpoint_are_provisional_until_resumed(monkeypatch):
    from tester.staged import StagedTester
    from tests.test_staged import candidates, config, install_probes, passing_sample

    calls = 0

    async def strict(proxy, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 5:
            raise asyncio.CancelledError
        return passing_sample(kwargs["dc_id"], kwargs["ping_count"])

    install_probes(monkeypatch, strict=strict)
    engine = StagedTester(config())
    with pytest.raises(asyncio.CancelledError):
        await engine.run_async(candidates(1))
    assert engine.partial_results[0].provisional
    assert engine.state["results"][0]["provisional"]
    install_probes(monkeypatch)
    resumed = StagedTester(config(), initial_state=engine.state)
    complete = await resumed.run_async(candidates(1))
    assert not complete[0].provisional
    assert not resumed.state["results"][0]["provisional"]


@pytest.mark.parametrize("mode", ["mtproto", "telegram", "web", "webproxy"])
def test_budget_exhaustion_during_collection_is_not_user_cancellation(tmp_path, monkeypatch, mode):
    from collector.collector import Collector
    from scan import run_scan
    from utils.scan_control import ScanStopped
    from webproxy.collector import WebCollector

    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\nwebproxy_sources: []\n")
    cfg = AppConfig.load(path)
    cfg.scan.time_budget_seconds = 1.2
    first = run_scan(cfg, mode)
    previous = (first.folder / "completed_snapshot.json").read_bytes()
    progress = json.loads((first.folder / "progress.json").read_text())
    assert not progress["resumable"]

    def expired(self):
        raise ScanStopped("budget")

    monkeypatch.setattr(WebCollector if mode == "web" else Collector, "collect", expired)
    result = run_scan(cfg, mode)
    assert result.summary["budget_exhausted"] and result.summary["interrupted"]
    assert result.summary["interrupted_stage"] == "collection"
    assert not result.summary["collection_complete"]
    assert result.summary["stop_reason"] == "budget"
    assert result.summary["coverage_percent"] == 0
    assert result.top == [] and result.summary["tested"] == 0
    assert (result.folder / "completed_snapshot.json").read_bytes() == previous
    progress = json.loads((result.folder / "progress.json").read_text())
    assert not progress["active"] and not progress["resumable"]


@pytest.mark.parametrize("slow_stage", ["history", "observations.json", "proxy.json"])
def test_finalization_rechecks_proof_after_slow_bulk_write(tmp_path, monkeypatch, slow_stage):
    from collector.collector import Collector
    from scan import run_scan
    from tester.runner import ParallelTester
    from utils.scan_state import HistoryStore

    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nscan:\n  strategy: single\n")
    cfg = AppConfig.load(path)
    now = time.time()
    result = qualified(stamp=now - 1799)
    monkeypatch.setattr(Collector, "collect", lambda self: [result.proxy])
    monkeypatch.setattr(ParallelTester, "run", lambda self, selected: [result])

    def slow_record(self, *args, **kwargs):
        monkeypatch.setattr("utils.evidence.time.time", lambda: now + 2)

    if slow_stage == "history":
        monkeypatch.setattr(HistoryStore, "record", slow_record)
    else:
        write = Exporter._write

        def slow_write(self, filename, content):
            if filename == slow_stage:
                monkeypatch.setattr("utils.evidence.time.time", lambda: now + 2)
            return write(self, filename, content)

        monkeypatch.setattr(Exporter, "_write", slow_write)
    outcome = run_scan(cfg)
    assert outcome.top == [] and outcome.eligible == []
    assert outcome.summary["expired_results"] == 1
    assert outcome.summary["displayed"] == outcome.summary["eligible"] == 0
    assert json.loads((outcome.folder / "snapshot.json").read_text())["rows"] == []
    assert json.loads((outcome.folder / "top10.json").read_text()) == []
    assert (outcome.folder / "telegram_links.txt").read_text() == ""


def test_row_serialization_uses_one_instant_across_expiry(monkeypatch):
    now = time.time()
    result = qualified(stamp=now - 1799)
    clock = iter([now, now + 2])
    monkeypatch.setattr("utils.models.time.time", lambda: next(clock))
    row = result.to_dict()
    assert row["evidence_status"] == "fresh" and row["strict_eligible"]
    assert saved_evidence_status(row, now=now + 2) == "expired"


def test_snapshot_expiring_during_atomic_publication_is_readable_but_not_actionable(tmp_path, monkeypatch):
    from collector.collector import Collector
    from dashboard.data import snapshot
    from scan import run_scan
    from tester.runner import ParallelTester

    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nscan:\n  strategy: single\n")
    cfg = AppConfig.load(path)
    now = time.time()
    result = qualified(stamp=now - 1799)
    monkeypatch.setattr(Collector, "collect", lambda self: [result.proxy])
    monkeypatch.setattr(ParallelTester, "run", lambda self, selected: [result])
    write = Exporter._write

    def slow_write(self, filename, content):
        if filename == "snapshot.json":
            monkeypatch.setattr("utils.evidence.time.time", lambda: now + 2)
        return write(self, filename, content)

    monkeypatch.setattr(Exporter, "_write", slow_write)
    outcome = run_scan(cfg)
    assert outcome.top == []
    saved = json.loads((outcome.folder / "snapshot.json").read_text())
    assert saved["rows"][0]["strict_eligible"]
    assert saved["summary"]["evidence_as_of"] < saved["rows"][0]["evidence_expires_at"]
    current = snapshot(cfg.output.folder)
    assert current["state"] == "ready"
    assert current["rows"][0]["evidence_status"] == "expired"
    assert current["rows"][0]["tg_link"] is None
