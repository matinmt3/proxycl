import json
from pathlib import Path

import pytest

from config.loader import AppConfig
from utils.interruption import ScanInterrupted
from utils.models import FailureReason, Proxy, ProxyResult, TestSample
from webproxy.models import WebProxy


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\n", encoding="utf-8")
    return AppConfig.load(path)


@pytest.mark.parametrize("mode", ["mtproto", "telegram", "web"])
def test_interrupted_scan_exports_only_real_samples_and_preserves_completed_generation(
    config, monkeypatch, mode
):
    from collector.collector import Collector
    from scan import run_scan
    from tester.runner import ParallelTester
    from webproxy.collector import WebCollector
    from webproxy.tester import WebTester

    collector, tester = (WebCollector, WebTester) if mode == "web" else (Collector, ParallelTester)
    proxies = [
        (
            WebProxy(f"proxy-{n}.example", 8080)
            if mode == "web"
            else Proxy(f"proxy-{n}.example", 443, "00112233445566778899aabbccddeeff")
        )
        for n in range(30)
    ]
    monkeypatch.setattr(collector, "collect", lambda self: proxies)
    monkeypatch.setattr(
        tester,
        "run",
        lambda self, selected: [
            ProxyResult(p, [TestSample(True, total_latency_ms=100)] * 3) for p in selected
        ],
    )
    complete = run_scan(config, mode, 2)
    previous = (complete.folder / "snapshot.json").read_bytes()

    partial = [
        ProxyResult(p, [TestSample(True, total_latency_ms=200 - i * 10)] * 3)
        for i, p in enumerate(proxies[:14])
    ]
    partial[0].samples = [TestSample(False, failure_reason=FailureReason.TCP_TIMEOUT)] * 3
    partial.append(ProxyResult(proxies[14], [TestSample(True, total_latency_ms=50)]))

    def stop(self, selected):
        raise ScanInterrupted(partial)

    monkeypatch.setattr(tester, "run", stop)
    outcome = run_scan(config, mode)
    assert outcome.summary["interrupted"] is True
    assert outcome.summary["selected"] == 30 and outcome.summary["tested"] == 15
    assert outcome.summary["fully_tested"] == 14 and outcome.summary["partial_tested"] == 1
    assert outcome.summary["retry_count"] == 3 and outcome.summary["skipped"] == 15
    assert outcome.summary["verified"] == 14 and outcome.summary["displayed"] == 10
    assert "Stopped scan / partial results" in (outcome.folder / "best10_links.txt").read_text()
    assert (outcome.folder / "completed_snapshot.json").read_bytes() == previous
    snap = json.loads((outcome.folder / "snapshot.json").read_text())
    assert snap["summary"] == outcome.summary
    assert snap["rows"][:10] == json.loads((outcome.folder / "top10.json").read_text())
    assert {r["server"] for r in snap["rows"]} == {p.server for p in proxies[1:15]}
    assert not (Path(config.output.folder) / ("mtproto" if mode != "mtproto" else "telegram")).exists()


def test_interrupt_before_any_finished_attempt_shows_empty_partial_and_keeps_history(
    config, monkeypatch, capsys
):
    import main
    from collector.collector import Collector
    from tester.runner import ParallelTester

    proxies = [Proxy("proxy.example", 443, "00112233445566778899aabbccddeeff")]
    monkeypatch.setattr(Collector, "collect", lambda self: proxies)

    def stop(self, candidates):
        raise ScanInterrupted([])

    monkeypatch.setattr(ParallelTester, "run", stop)
    main.cmd_best10(config)
    output = capsys.readouterr().out
    assert "Stopped" in output and "VERIFIED PROXIES: 0 / 10" in output
    snapshot = json.loads((Path(config.output.folder) / "mtproto" / "snapshot.json").read_text())
    assert snapshot["summary"]["tested"] == 0 and snapshot["summary"]["skipped"] == 1
    assert snapshot["rows"] == []
