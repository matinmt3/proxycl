import asyncio

import pytest

from config.loader import AppConfig
from tester.runner import ParallelTester
from tests.proxy_fixtures import qualified_result
from utils.models import Proxy, ProxyResult, TestSample
from webproxy.models import WebProxy
from webproxy.tester import WebTester


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\n")
    return AppConfig.load(path)


def test_mtproto_resume_keeps_finished_samples_and_rotates_remaining_dcs(config, monkeypatch):
    from tester import mtproto

    proxy = Proxy("saved.example", 443, "01" * 16)
    saved = qualified_result(proxy, attempts=1)
    first = saved.samples[0]
    calls, snapshots = [], []

    async def probe(proxy, **kwargs):
        calls.append(kwargs["dc_id"])
        sample = qualified_result(proxy).samples[0]
        sample.dc_id = kwargs["dc_id"]
        return sample

    async def checkpoint(result):
        await asyncio.sleep(0)
        snapshots.append(len(result.samples))

    monkeypatch.setattr(mtproto, "test_proxy_strict", probe)
    tester = ParallelTester(config)
    tester.initial_results, tester.on_sample = [saved], checkpoint
    results = tester.run([proxy])
    assert calls == [4, 2] and snapshots == [2, 3]
    assert results[0].samples[0] is first and results[0].strict_eligible


def test_web_resume_skips_fully_finished_candidates(config, monkeypatch):
    import webproxy.tester as module

    old, partial = WebProxy("old.example", 80), WebProxy("partial.example", 80)
    calls = []

    async def probe(proxy, *args, **kwargs):
        calls.append(proxy.server)
        return TestSample(True, total_latency_ms=100)

    monkeypatch.setattr(module, "test_proxy_once", probe)
    tester = WebTester(config)
    tester.initial_results = [
        ProxyResult(old, [TestSample(True, total_latency_ms=99)] * 3),
        ProxyResult(partial, [TestSample(False)]),
    ]
    results = tester.run([old, partial])
    assert calls == ["partial.example", "partial.example"]
    assert [result.attempts for result in results] == [3, 3]
    assert results[1].success_rate == pytest.approx(2 / 3)
