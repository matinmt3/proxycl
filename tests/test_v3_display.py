import main
from config.loader import AppConfig
from utils.models import FailureReason, Proxy, ProxyResult, TestSample


def test_benchmark_shows_only_verified_top10_and_does_not_write(tmp_path, monkeypatch, capsys):
    from collector.collector import Collector
    from tester.runner import ParallelTester

    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\n")
    cfg = AppConfig.load(path)
    dead = ProxyResult(
        Proxy("dead.example", 443, "00112233445566778899aabbccddeeff"),
        [TestSample(False, failure_reason=FailureReason.TCP_TIMEOUT)],
    )
    good = ProxyResult(
        Proxy("good.example", 443, "00112233445566778899aabbccddeeff"),
        [TestSample(True, total_latency_ms=10)],
    )
    monkeypatch.setattr(Collector, "collect", lambda self: [dead.proxy, good.proxy])
    monkeypatch.setattr(ParallelTester, "run", lambda self, proxies: [dead, good])
    main.cmd_benchmark(cfg)
    output = capsys.readouterr().out
    assert "good.example" in output and "dead.example" not in output
    assert not (tmp_path / "output").exists()
