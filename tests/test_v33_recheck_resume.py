"""Recheck must retain its original saved candidates after a stopped export."""

import asyncio
import json

import pytest

import main
from config.loader import AppConfig
from scan import mode_folder
from tests.proxy_fixtures import qualified_result
from utils.models import Proxy, TestSample
from webproxy.models import WebProxy


@pytest.mark.parametrize("mode", ["mtproto", "telegram", "web"])
def test_recheck_resumes_its_saved_candidate_file_after_partial_export(tmp_path, monkeypatch, mode):
    from tester import staged
    from webproxy import tester as web_tester

    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\n")
    config = AppConfig.load(path)
    config.scan.network = "mobile"
    config.scan.finalist_reconnect_delay_seconds = 0
    folder = mode_folder(config, mode)
    folder.mkdir(parents=True)
    original = (
        dict(WebProxy("saved.example", 1080, "socks5").to_dict(), attempts=3, successes=3)
        if mode == "web"
        else qualified_result(Proxy("saved.example", 443, "01" * 16)).to_dict()
    )
    (folder / "proxy.json").write_text(json.dumps([original]))
    calls = []

    async def screen(proxy, **kwargs):
        return TestSample(True)

    async def probe(proxy, *args, **kwargs):
        calls.append(proxy.server)
        if len(calls) == 2:
            raise asyncio.CancelledError
        if mode == "web":
            return TestSample(True, total_latency_ms=100)
        return TestSample(
            True,
            total_latency_ms=1000,
            verification_level="telegram_session",
            dc_id=kwargs["dc_id"],
            ping_rtts_ms=[100] * kwargs["ping_count"],
        )

    monkeypatch.setattr(staged, "test_proxy_once", screen)
    monkeypatch.setattr(staged, "test_proxy_strict", probe)
    monkeypatch.setattr(web_tester, "test_proxy_once", probe)
    assert main.cmd_recheck(config, mode=mode) is False
    candidate_file = folder / "recheck-candidates.txt"
    saved_candidates = candidate_file.read_bytes()
    stopped = json.loads((folder / "snapshot.json").read_text())
    assert stopped["summary"]["interrupted"] is True
    assert stopped["summary"]["command"] == "recheck"

    config.scan.resume = True
    assert main.cmd_recheck(config, mode=mode) is True
    resumed = json.loads((folder / "snapshot.json").read_text())
    assert candidate_file.read_bytes() == saved_candidates
    assert resumed["summary"]["resumed"] is True
    assert resumed["summary"]["run_id"] == stopped["summary"]["run_id"]
    assert resumed["summary"]["command"] == "recheck"
    assert resumed["rows"][0]["attempts"] == (3 if mode == "web" else 8)
    assert set(calls) == {"saved.example"}
    assert config.sources == config.web_sources == []
