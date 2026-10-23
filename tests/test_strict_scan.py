import json

import pytest

from config.loader import AppConfig
from tests.proxy_fixtures import qualified_result
from utils.interruption import ScanInterrupted
from utils.models import Proxy, ProxyResult, TestSample


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\nscan:\n  strategy: single\n", encoding="utf-8")
    return AppConfig.load(path)


@pytest.mark.parametrize("mode", ["mtproto", "telegram"])
def test_strict_scan_does_not_promote_quick_probe_and_keeps_actual_evidence(config, monkeypatch, mode):
    from collector.collector import Collector
    from scan import run_scan
    from tester.runner import ParallelTester

    weak = ProxyResult(Proxy("quick.example", 443, "01" * 16), [TestSample(True, total_latency_ms=1)])
    reliable = qualified_result(Proxy("reliable.example", 443, "02" * 16), 150)
    monkeypatch.setattr(Collector, "collect", lambda self: [weak.proxy, reliable.proxy])

    def interrupted(self, candidates):
        raise ScanInterrupted([weak, reliable])

    monkeypatch.setattr(ParallelTester, "run", interrupted)
    outcome = run_scan(config, mode)
    assert outcome.summary["verification_profile"] == "telegram_session"
    assert outcome.summary["transport_reachable"] == 2
    assert outcome.summary["strict_verified"] == outcome.summary["verified"] == 1
    assert outcome.summary["interrupted"] is True
    assert [r.proxy.server for r in outcome.top] == ["reliable.example"]
    observed = json.loads((outcome.folder / "observations.json").read_text())
    # Raw evidence is persisted before final recommendation publication. The
    # latter must recheck freshness rather than renew the observation timestamp.
    assert observed["summary"]["evidence_as_of"] <= outcome.summary["evidence_as_of"]
    assert {key: value for key, value in observed["summary"].items() if key != "evidence_as_of"} == {
        key: value for key, value in outcome.summary.items() if key != "evidence_as_of"
    }
    published = json.loads((outcome.folder / "snapshot.json").read_text())
    assert published["summary"] == outcome.summary
    assert published["rows"][0]["evidence_expires_at"] > outcome.summary["evidence_as_of"]
    assert [row["server"] for row in observed["rows"]] == [r.proxy.server for r in outcome.results]
    assert sorted(len(row["samples"]) for row in observed["rows"]) == [1, 3]
    assert "auth_key" not in (outcome.folder / "observations.json").read_text()
    assert "quick.example" not in (outcome.folder / "best10_links.txt").read_text()


def test_top_does_not_offer_legacy_transport_results_as_strict(config, capsys):
    import main
    from scan import mode_folder

    folder = mode_folder(config, "telegram")
    folder.mkdir(parents=True)
    row = ProxyResult(
        Proxy("old-quick.example", 443, "03" * 16), [TestSample(True, total_latency_ms=1)]
    ).to_dict()
    (folder / "proxy.json").write_text(json.dumps([row]))
    main.cmd_top(config, 10, mode="telegram")
    output = capsys.readouterr().out
    assert "old-quick.example" not in output
    assert "strict" in output.lower() and "best10" in output
