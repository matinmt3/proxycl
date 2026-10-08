import json
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from dashboard import data, server


@pytest.fixture
def records(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            [
                {
                    "server": "b.test",
                    "port": 443,
                    "score": 0.5,
                    "success_rate": 0.5,
                    "avg_latency_ms": 50,
                    "source": "manual",
                },
                {
                    "server": "a.test",
                    "port": 443,
                    "score": 0.9,
                    "success_rate": 1,
                    "avg_latency_ms": 10,
                    "source": "feed",
                },
                {"server": "c.test", "port": 443, "score": 0, "success_rate": 0, "avg_latency_ms": None},
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_dashboard_filter_sort_stats(records):
    assert [row["server"] for row in data.proxies(records)] == ["a.test", "b.test", "c.test"]
    assert [row["server"] for row in data.proxies(records, sort_by="avg_latency_ms", order="asc")] == [
        "a.test",
        "b.test",
        "c.test",
    ]
    assert len(data.proxies(records, search="MANUAL")) == 1
    stats = data.stats(records)
    assert stats["healthy_proxies"] == 2 and stats["average_latency_ms"] == 30
    assert stats["best_proxy"]["server"] == "a.test"


@pytest.mark.parametrize(
    "body", ["", "{broken", "{}", "null", '[null,1,"text",{}]', '[{"server":"x","score":NaN}]']
)
def test_dashboard_invalid_file_is_empty(tmp_path, body):
    path = tmp_path / "proxy.json"
    path.write_text(body)
    assert data.proxies(path) == []
    assert data.stats(path)["total_proxies"] == 0


def test_stdlib_server_http_routes_and_bad_queries(records):
    httpd = server.create_server("127.0.0.1", 0, records.parent)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with urlopen(base + "/") as response:
            assert b"REVMAMAD" in response.read()
        with urlopen(base + "/dashboard.js") as response:
            javascript = response.read()
            assert b"textContent" in javascript and b"/api/snapshot" in javascript
            assert b"Chart" not in javascript  # no external chart runtime in offline v3
        with urlopen(base + "/api/proxies?search=feed") as response:
            assert json.load(response)[0]["server"] == "a.test"
        with urlopen(base + "/api/stats") as response:
            assert json.load(response)["total_proxies"] == 3
        for query in ("limit=bad", "limit=0", "sort_by=source", "order=wrong"):
            with pytest.raises(HTTPError) as error:
                urlopen(base + "/api/proxies?" + query)
            assert error.value.code == 400
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/../main.py")
        assert error.value.code == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_termux_browser_opener(monkeypatch):
    calls = []
    monkeypatch.setattr(server.shutil, "which", lambda name: "/bin/termux-open-url")
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kwargs: calls.append(args))
    assert server.open_dashboard("http://127.0.0.1:8000")
    assert calls == [["/bin/termux-open-url", "http://127.0.0.1:8000"]]


def test_optional_fastapi_routes(records, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard import app as adapter

    monkeypatch.setattr(adapter, "DATA_FILE", records)
    client = TestClient(adapter.app)
    assert client.get("/api/proxies?sort_by=bad").status_code == 422
    assert client.get("/api/stats").json()["healthy_proxies"] == 2
    assert client.get("/api/proxies?order=asc&sort_by=avg_latency_ms").json()[-1]["server"] == "c.test"
    assert "REVMAMAD" in client.get("/").text
