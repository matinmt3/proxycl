import json

from config.loader import OutputConfig
from utils.models import ProxyResult, TestSample


def results():
    from webproxy.models import WebProxy

    # Score takes precedence over latency; keys settle identical metrics.
    rows = []
    for server, latency, score in [
        ("z.example", 5, 0.4),
        ("b.example", 50, 0.9),
        ("a.example", 50, 0.9),
        ("dead.example", None, 0.99),
    ]:
        row = ProxyResult(
            WebProxy(server, 8080, "http"), [TestSample(latency is not None, total_latency_ms=latency)]
        )
        row.score = score
        rows.append(row)
    return rows


def test_web_exports_share_score_latency_key_order_without_telegram_fields(tmp_path):
    from webproxy.exporter import WebExporter

    exporter = WebExporter(OutputConfig(folder=str(tmp_path), top_sizes=[10]))
    paths = exporter.export_all(results())
    best = exporter.export_best_n_readable(results())
    rows = json.loads((tmp_path / "proxy.json").read_text())
    assert [r["server"] for r in rows] == ["a.example", "b.example", "z.example"]
    assert json.loads((tmp_path / "top10.json").read_text()) == rows
    assert (tmp_path / "proxy.txt").read_text().splitlines()[0].startswith("http://a.example:8080")
    readable = best.read_text()
    assert (
        readable.index("http://a.example:8080")
        < readable.index("http://b.example:8080")
        < readable.index("http://z.example:8080")
    )
    assert "protocol" in (tmp_path / "proxy.csv").read_text().splitlines()[0]
    assert "secret" not in json.dumps(rows) and "tg_link" not in json.dumps(rows)
    assert "telegram_links" not in paths


def test_empty_web_export_clears_its_previous_completed_files(tmp_path):
    from webproxy.exporter import WebExporter

    exporter = WebExporter(OutputConfig(folder=str(tmp_path), top_sizes=[10]))
    exporter.export_all(results())
    exporter.export_all([])
    exporter.export_best_n_readable([])
    assert json.loads((tmp_path / "proxy.json").read_text()) == []
    assert json.loads((tmp_path / "top10.json").read_text()) == []
    assert (tmp_path / "proxy.txt").read_text() == ""
    assert "a.example" not in (tmp_path / "best10_links.txt").read_text()


def test_web_exporter_retains_atomic_writer_and_forced_top10_contract(tmp_path):
    from exporter.exporter import Exporter
    from webproxy.exporter import WebExporter

    exporter = WebExporter(OutputConfig(folder=str(tmp_path), top_sizes=[]))
    assert isinstance(exporter, Exporter)
    exporter.export_all(results())
    path = exporter.export_top_n(results()[:1], 10)
    assert json.loads(path.read_text())[0]["uri"] == "http://z.example:8080"
    assert not list(tmp_path.glob(".revmamad-*"))
