import json
from pathlib import Path

import pytest

from config.loader import AppConfig
from utils.models import Proxy, ProxyResult, TestSample


def proxy(host, source="a"):
    return Proxy(host, 443, "00112233445566778899aabbccddeeff", source=source)


def test_limit_follows_global_dedupe_and_interleaves_sources():
    from scan import select_candidates

    rows = [
        proxy("1.1.1.1"),
        proxy("2.2.2.2"),
        proxy("1.1.1.1", "b"),
        proxy("3.3.3.3", "b"),
        proxy("4.4.4.4", "c"),
    ]
    assert [p.server for p in select_candidates(rows, 3)] == ["1.1.1.1", "3.3.3.3", "4.4.4.4"]
    assert len(select_candidates(rows, None)) == 4
    assert len(select_candidates(rows, 50)) == 4


@pytest.mark.parametrize("limit", [0, -1, True, 2.5, "ALL"])
def test_invalid_limit_rejected_before_any_collection(limit):
    from scan import select_candidates

    with pytest.raises(ValueError):
        select_candidates([], limit)


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("sources: []\n", encoding="utf-8")
    return AppConfig.load(path)


def test_scan_selected_denominator_top10_and_snapshot_agree(cfg, monkeypatch):
    from collector.collector import Collector
    from scan import run_scan
    from tester.runner import ParallelTester

    rows = [proxy(f"8.8.8.{n}", str(n % 3)) for n in range(1, 16)]
    monkeypatch.setattr(Collector, "collect", lambda self: rows)
    selected = []

    def probes(self, candidates):
        selected.extend(candidates)
        return [
            ProxyResult(p, [TestSample(True, total_latency_ms=100 + i)]) for i, p in enumerate(candidates)
        ]

    monkeypatch.setattr(ParallelTester, "run", probes)
    outcome = run_scan(cfg, "mtproto", 12)
    assert len(selected) == 12
    assert outcome.summary["collected"] == 15 and outcome.summary["selected"] == 12
    assert outcome.summary["tested"] == 12 and outcome.summary["displayed"] == 10
    folder = Path(cfg.output.folder) / "mtproto"
    snapshot = json.loads((folder / "snapshot.json").read_text())
    top10 = json.loads((folder / "top10.json").read_text())
    assert snapshot["mode"] == "mtproto" and snapshot["summary"] == outcome.summary
    assert [row["server"] for row in top10] == [r.proxy.server for r in outcome.top]
    assert snapshot["rows"][:10] == top10
    assert not (Path(cfg.output.folder) / "web").exists()


def test_completed_empty_scan_clears_only_its_mode(cfg, monkeypatch):
    from collector.collector import Collector
    from scan import run_scan

    folder = Path(cfg.output.folder)
    (folder / "web").mkdir(parents=True)
    (folder / "web" / "snapshot.json").write_text('{"preserve":"web"}')
    (folder / "mtproto").mkdir()
    (folder / "mtproto" / "proxy.json").write_text('[{"stale":true}]')
    monkeypatch.setattr(Collector, "collect", lambda self: [])
    outcome = run_scan(cfg)
    assert outcome.summary["tested"] == 0 and outcome.top == []
    assert json.loads((folder / "mtproto" / "proxy.json").read_text()) == []
    assert json.loads((folder / "mtproto" / "snapshot.json").read_text())["rows"] == []
    assert json.loads((folder / "web" / "snapshot.json").read_text()) == {"preserve": "web"}


def test_cancelled_scan_keeps_previous_completed_snapshot(cfg, monkeypatch):
    from collector.collector import Collector
    from scan import run_scan
    from tester.runner import ParallelTester

    folder = Path(cfg.output.folder) / "mtproto"
    folder.mkdir(parents=True)
    (folder / "snapshot.json").write_text('{"previous":true}')
    monkeypatch.setattr(Collector, "collect", lambda self: [proxy("1.1.1.1")])

    def cancel(self, proxies):
        raise KeyboardInterrupt

    monkeypatch.setattr(ParallelTester, "run", cancel)
    with pytest.raises(KeyboardInterrupt):
        run_scan(cfg)
    assert json.loads((folder / "snapshot.json").read_text()) == {"previous": True}


def test_bad_mode_rejected_before_loading_scanner_dependencies(cfg):
    from scan import run_scan

    with pytest.raises(ValueError, match="mode"):
        run_scan(cfg, "not-a-mode")


def test_top10_is_saved_even_when_other_top_sizes_are_configured(cfg, monkeypatch):
    from collector.collector import Collector
    from scan import run_scan
    from tester.runner import ParallelTester

    cfg.output.top_sizes = [2]
    rows = [proxy("1.1.1.1")]
    monkeypatch.setattr(Collector, "collect", lambda self: rows)
    monkeypatch.setattr(
        ParallelTester, "run", lambda self, p: [ProxyResult(p[0], [TestSample(True, total_latency_ms=10)])]
    )
    outcome = run_scan(cfg)
    assert json.loads((outcome.folder / "top10.json").read_text())[0]["server"] == "1.1.1.1"


def test_completed_scans_have_unique_identity_even_with_the_same_clock(cfg, monkeypatch):
    from uuid import UUID

    import scan
    from collector.collector import Collector

    monkeypatch.setattr(Collector, "collect", lambda self: [])
    monkeypatch.setattr(scan, "_now", lambda: "2026-10-08T12:00:00+00:00")
    first = scan.run_scan(cfg)
    second = scan.run_scan(cfg)
    assert UUID(first.summary["run_id"]) != UUID(second.summary["run_id"])
    assert (
        json.loads((second.folder / "snapshot.json").read_text())["summary"]["run_id"]
        == second.summary["run_id"]
    )
