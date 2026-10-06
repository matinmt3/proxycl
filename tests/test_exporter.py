import csv
import json

import pytest

from benchmark.scorer import score_result
from config.loader import OutputConfig, ScoringConfig
from exporter.exporter import Exporter
from utils.models import FailureReason, Proxy, ProxyResult
from utils.models import TestSample as Sample


def result(server="example.com", latency=25, success=True):
    item = ProxyResult(Proxy(server, 443, "01" * 16))
    item.samples = [
        Sample(
            success,
            total_latency_ms=latency if success else None,
            failure_reason=FailureReason.NONE if success else FailureReason.TCP_REFUSED,
        )
    ]
    item.score = score_result(item, ScoringConfig())
    return item


def test_dead_proxies_never_exported_as_healthy(tmp_path):
    exporter = Exporter(OutputConfig(folder=str(tmp_path)))
    bad = result(success=False)
    assert bad.score == 0
    paths = exporter.export_all([bad, result()])
    assert len(json.loads(tmp_path.joinpath("proxy.json").read_text())) == 1
    assert set(paths) >= {"json", "txt", "csv", "telegram_links", "top10"}
    assert len(list(csv.DictReader(tmp_path.joinpath("proxy.csv").open()))) == 1
    assert tmp_path.joinpath("telegram_links.txt").read_text().startswith("tg://proxy?")
    exporter.export_all([bad])
    assert json.loads(tmp_path.joinpath("proxy.json").read_text()) == []
    assert tmp_path.joinpath("telegram_links.txt").read_text() == ""


def test_best_applies_limits_and_sorts_speed(tmp_path):
    exporter = Exporter(OutputConfig(folder=str(tmp_path), max_latency_ms=100))
    exporter.export_best_n_readable(
        [result("slow.test", 500), result("second.test", 60), result("first.test", 10)]
    )
    text = tmp_path.joinpath("best10_links.txt").read_text()
    assert "up to 10 requested" in text and "slow.test" not in text
    assert text.index("first.test") < text.index("second.test")


def test_txt_ipv6_and_atomic_export(tmp_path):
    exporter = Exporter(OutputConfig(folder=str(tmp_path)))
    exporter.export_txt([result("2001:db8::1")])
    assert tmp_path.joinpath("proxy.txt").read_text().startswith("[2001:db8::1]:443:")
    assert not list(tmp_path.glob(".revmamad-*"))
    with pytest.raises(ValueError):
        exporter.export_json([], "../escape.json")
    with pytest.raises(ValueError):
        exporter.export_top_n([], 0)


def test_nonfinite_metrics_excluded(tmp_path):
    exporter = Exporter(OutputConfig(folder=str(tmp_path)))
    item = result(latency=float("nan"))
    assert item.score == 0
    exporter.export_all([item])
    assert json.loads(tmp_path.joinpath("proxy.json").read_text()) == []
