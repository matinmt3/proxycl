"""Web scan contracts exercise real selection, scoring and atomic exporters."""

import json
import re
from pathlib import Path

import pytest

from config.loader import AppConfig
from utils.models import FailureReason, ProxyResult, TestSample
from webproxy.collector import WebCollector
from webproxy.models import WebProxy
from webproxy.tester import WebTester


@pytest.fixture
def cfg(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("sources: []\nweb_sources: []\noutput:\n  top_sizes: []\n", encoding="utf-8")
    return AppConfig.load(path)


def boundary_candidates():
    return [
        WebProxy("z.example", 80, "http", "first"),
        WebProxy("later.example", 1080, "socks5", "first"),
        WebProxy("z.example", 80, "http", "second"),
        WebProxy("a.example", 443, "https", "second"),
        WebProxy("failed.example", 1080, "socks4", "third"),
    ]


def fake_collection(self):
    self.source_reports = [
        {"name": "first", "status": "ok", "count": 2},
        {"name": "second", "status": "ok", "count": 2},
        {"name": "third", "status": "ok", "count": 1},
    ]
    return boundary_candidates()


def fake_transport_results(self, candidates):
    rows = []
    for proxy in candidates:
        assert isinstance(proxy, WebProxy)
        if proxy.server == "failed.example":
            samples = [TestSample(False, failure_reason=FailureReason.HANDSHAKE_INVALID)]
        else:
            samples = [TestSample(True, tcp_latency_ms=5, handshake_latency_ms=15, total_latency_ms=20)]
        rows.append(ProxyResult(proxy, samples))
    return rows


def test_web_scan_dispatch_deduplicates_and_limits_before_shared_scoring_and_exports(cfg, monkeypatch):
    from collector.collector import Collector
    from scan import run_scan

    def wrong_mode(self):
        raise AssertionError("Web scan dispatched the MTProto collector")

    monkeypatch.setattr(Collector, "collect", wrong_mode)
    monkeypatch.setattr(WebCollector, "collect", fake_collection)
    monkeypatch.setattr(WebTester, "run", fake_transport_results)
    other = Path(cfg.output.folder) / "mtproto"
    other.mkdir(parents=True)
    (other / "snapshot.json").write_bytes(b'{"prior":"mtproto"}')

    outcome = run_scan(cfg, "web", limit=3)

    assert [r.proxy.uri() for r in outcome.results] == [
        "http://z.example:80",
        "https://a.example:443",
        "socks4://failed.example:1080",
    ]
    assert outcome.summary["collected"] == 4
    assert outcome.summary["requested_count"] == outcome.summary["selected"] == outcome.summary["tested"] == 3
    assert outcome.summary["verified"] == outcome.summary["eligible"] == outcome.summary["displayed"] == 2
    assert outcome.summary["failure_counts"] == {"handshake_invalid": 1}
    assert outcome.source_reports == [
        {"name": "first", "status": "ok", "count": 2},
        {"name": "second", "status": "ok", "count": 2},
        {"name": "third", "status": "ok", "count": 1},
    ]

    assert outcome.folder == Path(cfg.output.folder) / "web"
    snapshot = json.loads((outcome.folder / "snapshot.json").read_text())
    top10 = json.loads((outcome.folder / "top10.json").read_text())
    assert snapshot["mode"] == "web" and snapshot["summary"] == outcome.summary
    assert [r["uri"] for r in top10] == ["http://z.example:80", "https://a.example:443"]
    assert snapshot["rows"] == top10 == [r.to_dict() for r in outcome.top]
    readable = (outcome.folder / "best10_links.txt").read_text()
    assert readable.index("http://z.example:80") < readable.index("https://a.example:443")
    assert "failed.example" not in readable and "later.example" not in readable
    assert all("secret" not in row and "tg_link" not in row for row in top10)
    assert not (outcome.folder / "telegram_links.txt").exists()
    assert (other / "snapshot.json").read_bytes() == b'{"prior":"mtproto"}'


def test_completed_empty_web_scan_replaces_only_web_results_without_running_probes(cfg, monkeypatch):
    from scan import run_scan

    root = Path(cfg.output.folder)
    (root / "web").mkdir(parents=True)
    (root / "mtproto").mkdir()
    (root / "web" / "proxy.txt").write_text("http://old.example:80")
    (root / "web" / "top10.json").write_text('[{"prior":"web"}]')
    (root / "mtproto" / "snapshot.json").write_bytes(b'{"keep":"mtproto"}')

    def empty(self):
        self.source_reports = [
            {"name": "unavailable", "status": "error", "count": 0, "error": "TimeoutException"}
        ]
        return []

    def probes_must_not_run(self, candidates):
        raise AssertionError("Empty collection invoked transport testing")

    monkeypatch.setattr(WebCollector, "collect", empty)
    monkeypatch.setattr(WebTester, "run", probes_must_not_run)
    outcome = run_scan(cfg, "web")
    assert outcome.top == [] and outcome.summary["tested"] == 0
    assert json.loads((root / "web" / "top10.json").read_text()) == []
    assert json.loads((root / "web" / "snapshot.json").read_text())["source_reports"][0]["status"] == "error"
    assert (root / "web" / "proxy.txt").read_text() == ""
    assert "old.example" not in (root / "web" / "best10_links.txt").read_text()
    assert (root / "mtproto" / "snapshot.json").read_bytes() == b'{"keep":"mtproto"}'


def test_web_scan_forces_top10_and_keeps_all_verified_rows_in_snapshot(cfg, monkeypatch):
    from scan import run_scan

    candidates = [WebProxy(f"proxy-{n:02}.example", 8080, "http", "feed") for n in range(12, 0, -1)]

    def collect(self):
        self.source_reports = [{"name": "feed", "status": "ok", "count": 12}]
        return candidates

    def probe(self, selected):
        return [
            ProxyResult(
                proxy,
                [
                    TestSample(
                        True,
                        tcp_latency_ms=2,
                        handshake_latency_ms=int(proxy.server.split("-")[1].split(".")[0]) * 10 - 2,
                        total_latency_ms=int(proxy.server.split("-")[1].split(".")[0]) * 10,
                    )
                ],
            )
            for proxy in selected
        ]

    monkeypatch.setattr(WebCollector, "collect", collect)
    monkeypatch.setattr(WebTester, "run", probe)
    outcome = run_scan(cfg, "web")

    expected = [f"http://proxy-{n:02}.example:8080" for n in range(1, 13)]
    snapshot = json.loads((outcome.folder / "snapshot.json").read_text())
    top10 = json.loads((outcome.folder / "top10.json").read_text())
    readable = (outcome.folder / "best10_links.txt").read_text()
    readable_rows = re.findall(r"^#(\d+)  (\S+)$", readable, flags=re.MULTILINE)
    readable_uris = [uri for _, uri in readable_rows]
    assert outcome.summary["requested_count"] is None
    assert outcome.summary["tested"] == outcome.summary["verified"] == outcome.summary["eligible"] == 12
    assert outcome.summary["displayed"] == 10
    assert [int(number) for number, _ in readable_rows] == list(range(1, 11))
    assert [row["uri"] for row in snapshot["rows"]] == expected
    assert (
        [row["uri"] for row in top10]
        == readable_uris
        == [row.proxy.uri() for row in outcome.top]
        == expected[:10]
    )


def test_cancelled_web_scan_preserves_previous_completed_generation(cfg, monkeypatch):
    from scan import run_scan

    folder = Path(cfg.output.folder) / "web"
    folder.mkdir(parents=True)
    previous = {
        "snapshot.json": b'{"old":"snapshot"}',
        "top10.json": b'[{"old":"top10"}]',
        "proxy.txt": b"http://old.example:80",
        "best10_links.txt": b"previous readable generation",
    }
    for filename, content in previous.items():
        (folder / filename).write_bytes(content)
    monkeypatch.setattr(WebCollector, "collect", fake_collection)

    def cancel(self, candidates):
        raise KeyboardInterrupt

    monkeypatch.setattr(WebTester, "run", cancel)
    with pytest.raises(KeyboardInterrupt):
        run_scan(cfg, "web", limit=1)
    assert {name: (folder / name).read_bytes() for name in previous} == previous


def test_web_benchmark_only_returns_verified_rank_without_creating_output(cfg, monkeypatch):
    from scan import run_scan

    monkeypatch.setattr(WebCollector, "collect", fake_collection)
    monkeypatch.setattr(WebTester, "run", fake_transport_results)
    outcome = run_scan(cfg, "web", limit=3, export=False)
    assert [r.proxy.uri() for r in outcome.top] == ["http://z.example:80", "https://a.example:443"]
    assert outcome.summary["eligible"] == 2
    assert not Path(cfg.output.folder).exists()
