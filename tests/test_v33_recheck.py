import asyncio
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from config.loader import AppConfig
from scan import mode_folder, run_scan
from tests.proxy_fixtures import qualified_result
from utils.models import Proxy, TestSample
from webproxy.models import WebProxy


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\n")
    return AppConfig.load(path)


@pytest.mark.parametrize("mode", ["mtproto", "telegram", "web"])
def test_recheck_uses_only_saved_candidates_on_the_selected_network(config, monkeypatch, mode):
    config.scan.network = "mobile"
    folder = mode_folder(config, mode)
    folder.mkdir(parents=True)
    if mode == "web":
        rows = [dict(WebProxy("saved.example", 1080, "socks5").to_dict(), attempts=3, successes=3)]
    else:
        rows = [qualified_result(Proxy("saved.example", 443, "01" * 16)).to_dict()]
    (folder / "proxy.json").write_text(json.dumps(rows))
    calls = []

    def run(prepared, **kwargs):
        from collector.collector import Collector
        from webproxy.collector import WebCollector

        collector = WebCollector(prepared) if mode == "web" else Collector(prepared)
        candidates = collector.collect()
        calls.append((candidates, kwargs, prepared._scan_command, prepared.scan.network))
        return True

    monkeypatch.setattr(main, "cmd_best10", run)
    assert main.cmd_recheck(config, mode=mode)
    assert len(calls[0][0]) == 1 and calls[0][0][0].server == "saved.example"
    assert calls[0][1] == {"mode": mode, "limit": None}
    assert calls[0][2:] == ("recheck", "mobile")
    assert config.sources == config.web_sources == []


def test_recheck_refuses_unproven_saved_mtproto(config, monkeypatch):
    folder = mode_folder(config, "mtproto")
    folder.mkdir(parents=True)
    (folder / "proxy.json").write_text(
        json.dumps([{"server": "weak.example", "port": 443, "secret": "01" * 16}])
    )
    monkeypatch.setattr(
        main, "cmd_best10", lambda *args, **kwargs: pytest.fail("unproven saved candidate was tested")
    )
    with pytest.raises(ValueError, match="No qualified"):
        main.cmd_recheck(config)


def test_single_resume_reconstructs_ping_count_policy_from_config(config, monkeypatch):
    from collector.collector import Collector
    from tester import mtproto

    config.scan.strategy = "single"
    config.testing.retries = 4
    config.testing.ping_count = 10
    candidate = Proxy("saved.example", 443, "01" * 16)
    monkeypatch.setattr(Collector, "collect", lambda self: [candidate])
    calls = []

    async def probe(p, **kwargs):
        calls.append(kwargs)
        if len(calls) == 4:
            raise asyncio.CancelledError
        return TestSample(
            True,
            total_latency_ms=1000,
            verification_level="telegram_session",
            dc_id=kwargs["dc_id"],
            ping_rtts_ms=[100] * 10,
        )

    monkeypatch.setattr(mtproto, "test_proxy_strict", probe)
    stopped = run_scan(config)
    assert stopped.summary["interrupted"] and stopped.top[0].attempts == 3
    path = Path(config.output.folder) / ".state" / "checkpoint-mtproto-default.json"
    checkpoint = json.loads(path.read_text())
    row = checkpoint["state"]["engine"]["results"][0]
    row["quality_policy"]["min_ping_count"] = 3
    for sample in row["samples"]:
        sample["ping_rtts_ms"] = [100] * 3
    path.write_text(json.dumps(checkpoint))
    config.scan.resume = True
    resumed = run_scan(config)
    assert resumed.results[0].quality_policy.min_ping_count == 10
    assert resumed.top == []
    assert resumed.results[0].strict_successes == 1


def test_fingerprint_change_refuses_resume_before_any_new_probe(config, monkeypatch):
    from collector.collector import Collector
    from tester import staged

    monkeypatch.setattr(Collector, "collect", lambda self: [Proxy("saved.example", 443, "01" * 16)])

    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(staged, "test_proxy_once", cancel)
    assert run_scan(config).summary["interrupted"]
    config.testing.ping_count = 4
    config.scan.resume = True
    with pytest.raises(ValueError, match="configuration changed"):
        run_scan(config)


def test_large_web_scan_selects_sqlite_and_completes_without_json_candidate_limit(config, monkeypatch):
    from webproxy.collector import WebCollector
    from webproxy.tester import WebTester

    candidates = [WebProxy(f"web-{i}.example", 80) for i in range(20_001)]
    monkeypatch.setattr(WebCollector, "collect", lambda self: candidates)
    monkeypatch.setattr(WebTester, "run", lambda self, selected: [])
    result = run_scan(config, "web")
    assert result.summary["selected"] == 20_001
    pointer = json.loads((Path(config.output.folder) / ".state" / "resume-web-default.json").read_text())
    assert pointer["backend"] == "sqlite"
    config.scan.resume = True
    with pytest.raises(ValueError, match="already complete"):
        run_scan(config, "web")


def test_single_checkpoint_writes_are_serialized_and_never_replace_newer_evidence(config, monkeypatch):
    import scan
    import tester.runner as runner
    from collector.collector import Collector
    from utils.scan_state import CheckpointStore

    config.scan.strategy = "single"
    config.testing.mtproto_profile = "transport"
    config.testing.retries, config.testing.workers = 1, 2
    candidates = [Proxy(f"saved-{i}.example", 443, "01" * 16) for i in range(2)]
    monkeypatch.setattr(Collector, "collect", lambda self: candidates)
    active = peak = 0
    counts = []
    guard = threading.Lock()
    original_save = CheckpointStore.save
    clock = iter(range(1, 1000))
    monkeypatch.setattr(scan, "time", SimpleNamespace(monotonic=lambda: next(clock) * 1.1))

    def save(store, proxies, state):
        nonlocal active, peak
        count = len(state["engine"]["results"])
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            if count == 1:
                time.sleep(0.1)
            original_save(store, proxies, state)
            counts.append(count)
        finally:
            with guard:
                active -= 1

    async def probe(proxy, **kwargs):
        await asyncio.sleep(0 if proxy is candidates[0] else 0.02)
        return TestSample(True, total_latency_ms=100)

    monkeypatch.setattr(CheckpointStore, "save", save)
    monkeypatch.setattr(runner, "test_proxy_once", probe)
    result = run_scan(config)
    assert result.summary["tested"] == 2
    assert peak == 1 and counts == sorted(counts)
    assert counts[0] == 0 and counts[-1] == 2
