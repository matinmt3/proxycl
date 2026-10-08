import json
import sys
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from dashboard import data, server


def write_snapshot(root, mode, rows, *, completed="2026-10-08T12:00:00+00:00"):
    folder = root / mode
    folder.mkdir(exist_ok=True)
    body = {
        "version": 1,
        "mode": mode,
        "summary": {
            "started_at": "2026-10-08T11:59:00+00:00",
            "completed_at": completed,
            "requested_count": None,
            "collected": 8,
            "selected": 8,
            "tested": 8,
            "verified": len(rows),
            "eligible": len(rows),
            "displayed": min(10, len(rows)),
            "failure_counts": {"tcp_timeout": 2},
        },
        "source_reports": [
            {"name": "feed", "status": "ok", "count": 8},
            {"name": "unavailable", "status": "error", "count": 0, "error": "HTTP 503"},
        ],
        "rows": rows,
    }
    (folder / "snapshot.json").write_text(json.dumps(body), encoding="utf-8")
    return body


@pytest.fixture
def snapshots(tmp_path):
    write_snapshot(
        tmp_path,
        "mtproto",
        [
            {
                "server": "telegram.example",
                "port": 443,
                "score": 0.9,
                "success_rate": 1,
                "avg_latency_ms": 20,
                "stability": 1,
                "secret": "00112233445566778899aabbccddeeff",
                "source": "feed",
                "tg_link": "tg://proxy?server=telegram.example&port=443&secret=00112233445566778899aabbccddeeff",
            }
        ],
    )
    write_snapshot(
        tmp_path,
        "web",
        [
            {
                "server": "http.example",
                "port": 8080,
                "protocol": "http",
                "uri": "http://http.example:8080",
                "score": 0.8,
                "success_rate": 1,
                "avg_latency_ms": 30,
                "stability": 1,
                "source": "feed",
            },
            {
                "server": "socks.example",
                "port": 1080,
                "protocol": "socks5",
                "uri": "socks5://socks.example:1080",
                "score": 0.8,
                "success_rate": 0.7,
                "avg_latency_ms": 50,
                "stability": 0.9,
                "source": "other",
            },
        ],
    )
    return tmp_path


def test_snapshot_mode_isolation_and_consistent_summary(snapshots):
    mtproto = data.snapshot(snapshots)
    web = data.snapshot(snapshots, "web")
    assert mtproto["state"] == web["state"] == "ready"
    assert mtproto["summary"]["eligible"] == len(mtproto["rows"]) == 1
    assert web["summary"]["eligible"] == len(web["rows"]) == 2
    assert mtproto["rows"][0]["server"] == "telegram.example"
    assert web["rows"][0]["server"] == "http.example"
    assert web["source_reports"][1]["status"] == "error"
    assert web["summary"]["completed_at"] == "2026-10-08T12:00:00+00:00"


def test_snapshot_missing_completed_empty_and_corrupt_are_distinct(tmp_path):
    assert data.snapshot(tmp_path, "web")["state"] == "missing"
    write_snapshot(tmp_path, "web", [])
    assert data.snapshot(tmp_path, "web")["state"] == "ready"
    assert data.snapshot(tmp_path, "web")["rows"] == []
    (tmp_path / "web" / "snapshot.json").write_text("broken", encoding="utf-8")
    assert data.snapshot(tmp_path, "web")["state"] == "corrupt"


@pytest.mark.parametrize(
    "change",
    [
        {"version": 99},
        {"version": True},
        {"mode": "mtproto"},
        {"rows": {}},
        {"summary": []},
        {"summary": {"eligible": "two"}},
        {"source_reports": {}},
    ],
)
def test_snapshot_rejects_wrong_schema_or_mode(tmp_path, change):
    body = write_snapshot(tmp_path, "web", [])
    body.update(change)
    (tmp_path / "web" / "snapshot.json").write_text(json.dumps(body), encoding="utf-8")
    assert data.snapshot(tmp_path, "web")["state"] == "corrupt"


def test_legacy_root_results_only_belong_to_mtproto_and_have_unknown_time(tmp_path):
    (tmp_path / "proxy.json").write_text('[{"server":"legacy.example","port":443,"score":0.8}]')
    old = data.snapshot(tmp_path, "mtproto")
    assert old["state"] == "legacy" and old["summary"]["completed_at"] is None
    assert old["summary"]["selected"] is None
    assert data.snapshot(tmp_path, "web")["state"] == "missing"


def test_corrupt_legacy_results_are_not_reported_as_a_completed_empty_scan(tmp_path):
    (tmp_path / "proxy.json").write_text("{broken", encoding="utf-8")
    assert data.snapshot(tmp_path)["state"] == "corrupt"


def test_stats_include_all_results_beyond_the_api_default_limit(tmp_path):
    path = tmp_path / "proxy.json"
    path.write_text(
        json.dumps(
            [
                {
                    "server": f"proxy-{index}.example",
                    "port": 443,
                    "score": index / 1000,
                    "success_rate": 1,
                    "avg_latency_ms": 30,
                }
                for index in range(201)
            ]
        ),
        encoding="utf-8",
    )
    current = data.stats(path)
    assert current["total_proxies"] == 201
    assert current["best_proxy"]["server"] == "proxy-200.example"
    assert current["worst_proxy"]["server"] == "proxy-0.example"


@pytest.mark.parametrize("mode", ["unknown", "../web", "web/snapshot.json", "", None])
def test_invalid_mode_is_rejected_before_file_access(tmp_path, mode):
    with pytest.raises(ValueError, match="mode"):
        data.snapshot(tmp_path, mode)


def test_snapshot_filter_order_and_top10_default(snapshots):
    assert len(data.proxies(snapshots, mode="web", protocol="socks5")) == 1
    assert data.proxies(snapshots, mode="web", search="OTHER")[0]["server"] == "socks.example"
    assert [row["server"] for row in data.proxies(snapshots, mode="web")] == ["http.example", "socks.example"]
    with pytest.raises(ValueError):
        data.proxies(snapshots, mode="mtproto", protocol="socks5")
    with pytest.raises(ValueError):
        data.proxies(snapshots, mode="web", protocol="javascript")


def test_equal_score_and_latency_use_the_full_transport_endpoint_key(tmp_path):
    write_snapshot(
        tmp_path,
        "web",
        [
            {
                "server": "z.example",
                "port": 80,
                "protocol": "http",
                "uri": "http://z.example:80",
                "score": 0.8,
                "avg_latency_ms": 30,
            },
            {
                "server": "a.example",
                "port": 1080,
                "protocol": "socks5",
                "uri": "socks5://a.example:1080",
                "score": 0.8,
                "avg_latency_ms": 30,
            },
        ],
    )
    assert [row["uri"] for row in data.proxies(tmp_path, mode="web")] == [
        "http://z.example:80",
        "socks5://a.example:1080",
    ]
    assert data.stats(tmp_path, mode="web")["best_proxy"]["server"] == "z.example"
    rows = [
        {"server": "same.example", "port": 443, "secret": secret, "score": 0.8, "avg_latency_ms": 30}
        for secret in ("1" * 32, "0" * 32)
    ]
    (tmp_path / "proxy.json").write_text(json.dumps(rows), encoding="utf-8")
    assert data.proxies(tmp_path / "proxy.json")[0]["secret"] == "0" * 32


def test_snapshot_preserves_optional_completed_run_id(snapshots):
    path = snapshots / "web" / "snapshot.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["summary"]["run_id"] = "new-completed-scan"
    path.write_text(json.dumps(body), encoding="utf-8")
    assert data.snapshot(snapshots, "web")["summary"]["run_id"] == "new-completed-scan"


@pytest.mark.parametrize(
    "change",
    [
        {"success_rate": 0},
        {"success_rate": -1},
        {"success_rate": True},
        {"score": -1},
        {"score": None},
        {"score": float("inf")},
        {"avg_latency_ms": -1},
        {"avg_latency_ms": None},
        {"avg_latency_ms": float("nan")},
        {"stability": -1},
        {"timeout_rate": 2},
        {"successes": 0},
    ],
)
def test_v3_snapshot_rejects_explicit_failed_or_invalid_health_rows(snapshots, change):
    path = snapshots / "web" / "snapshot.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["rows"][0].update(change)
    path.write_text(json.dumps(body), encoding="utf-8")
    assert data.snapshot(snapshots, "web")["state"] == "corrupt"


@pytest.mark.parametrize(
    "change",
    [
        {"eligible": 3},
        {"eligible": 1},
        {"displayed": 3},
        {"displayed": 1},
    ],
)
def test_snapshot_counts_must_match_its_saved_eligible_rows(snapshots, change):
    path = snapshots / "web" / "snapshot.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    body["summary"].update(change)
    path.write_text(json.dumps(body), encoding="utf-8")
    assert data.snapshot(snapshots, "web")["state"] == "corrupt"


def test_snapshot_default_order_preserves_full_precision_producer_ranking(tmp_path):
    # Producer scores .80004 and .80001 both serialize as .8. Its published
    # order remains authoritative; only an explicit latency sort may reverse it.
    write_snapshot(
        tmp_path,
        "web",
        [
            {
                "server": "score-winner.example",
                "port": 80,
                "protocol": "http",
                "uri": "http://score-winner.example:80",
                "score": 0.8,
                "avg_latency_ms": 100,
                "success_rate": 1,
            },
            {
                "server": "latency-winner.example",
                "port": 80,
                "protocol": "http",
                "uri": "http://latency-winner.example:80",
                "score": 0.8,
                "avg_latency_ms": 10,
                "success_rate": 1,
            },
        ],
    )
    assert data.proxies(tmp_path, mode="web")[0]["server"] == "score-winner.example"
    assert data.stats(tmp_path, mode="web")["best_proxy"]["server"] == "score-winner.example"
    assert (
        data.proxies(tmp_path, mode="web", sort_by="avg_latency_ms", order="asc")[0]["server"]
        == "latency-winner.example"
    )
    assert (
        data.api_response("/api/proxies", tmp_path, {"mode": ["web"]})[0]["server"] == "score-winner.example"
    )


@pytest.mark.parametrize("mode", ["mtproto", "web"])
def test_deeply_nested_corrupt_json_remains_a_dashboard_state(tmp_path, mode):
    folder = tmp_path / mode
    folder.mkdir()
    depth = sys.getrecursionlimit() + 100
    (folder / "snapshot.json").write_text("[" * depth + "0" + "]" * depth, encoding="utf-8")
    assert data.snapshot(tmp_path, mode)["state"] == "corrupt"


def test_actions_are_allowlisted_endpoint_matched_and_mode_specific(tmp_path):
    rows = [
        {
            "server": "safe.example",
            "port": 80,
            "protocol": "http",
            "uri": "javascript:alert(1)",
            "tg_link": "tg://proxy?fake=1",
        },
        {"server": "safe.example", "port": 80, "protocol": "http", "uri": "http://other.example:80"},
        {
            "server": "safe.example",
            "port": 80,
            "protocol": "http",
            "uri": "http://user:password@safe.example:80",
        },
        {"server": "safe.example", "port": 80, "protocol": "http", "uri": "http://safe.example:80"},
    ]
    write_snapshot(tmp_path, "web", rows)
    safe = data.snapshot(tmp_path, "web")["rows"]
    assert all("tg_link" not in row for row in safe)
    assert [row.get("uri") for row in safe] == [None, None, None, "http://safe.example:80"]
    write_snapshot(
        tmp_path,
        "mtproto",
        [
            {
                "server": "safe.example",
                "port": 443,
                "uri": "http://safe.example:443",
                "tg_link": "tg://proxy?server=other.example&port=443&secret=00112233445566778899aabbccddeeff",
            }
        ],
    )
    row = data.snapshot(tmp_path)["rows"][0]
    assert row.get("tg_link") is None and "uri" not in row


@pytest.fixture
def running_server(snapshots):
    httpd = server.create_server("127.0.0.1", 0, snapshots)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join(timeout=2)


def test_stdlib_snapshot_and_compatibility_apis(running_server):
    with urlopen(running_server + "/api/snapshot?mode=web", timeout=3) as response:
        result = json.load(response)
        assert response.headers["Cache-Control"] == "no-store"
        assert result["mode"] == "web" and len(result["rows"]) == 2
        assert result["summary"]["eligible"] == 2
    with urlopen(running_server + "/api/proxies", timeout=3) as response:
        assert json.load(response)[0]["server"] == "telegram.example"
    with urlopen(running_server + "/api/proxies?mode=web&protocol=socks5", timeout=3) as response:
        assert json.load(response)[0]["server"] == "socks.example"
    with urlopen(running_server + "/api/stats?mode=web", timeout=3) as response:
        assert json.load(response)["total_proxies"] == 2


@pytest.mark.parametrize(
    "path",
    [
        "/api/snapshot?mode=../web",
        "/api/snapshot?mode=web&mode=mtproto",
        "/api/proxies?mode=web&protocol=ssh",
        "/api/proxies?limit=1&limit=2",
    ],
)
def test_stdlib_rejects_invalid_and_ambiguous_queries(running_server, path):
    with pytest.raises(HTTPError) as error:
        urlopen(running_server + path, timeout=3)
    assert error.value.code == 400


def test_local_assets_are_offline_and_no_job_or_file_api_exists(running_server):
    for asset in ("/", "/styles.css", "/dashboard.js"):
        with urlopen(running_server + asset, timeout=3) as response:
            content = response.read().decode("utf-8")
        assert "cdn.jsdelivr" not in content and "fonts.googleapis" not in content
    for path in ("/api/jobs", "/api/scan", "/../main.py"):
        with pytest.raises(HTTPError) as error:
            urlopen(running_server + path, timeout=3)
        assert error.value.code == 404


def test_fastapi_adapter_matches_stdlib_snapshot(snapshots, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard import app as adapter

    monkeypatch.setattr(adapter, "OUTPUT_FOLDER", snapshots)
    client = TestClient(adapter.app)
    result = client.get("/api/snapshot?mode=web")
    assert result.status_code == 200
    assert result.json() == data.snapshot(snapshots, "web")
    assert client.get("/api/proxies?mode=web&protocol=socks5").json()[0]["server"] == "socks.example"
    assert client.get("/api/snapshot?mode=bad").status_code in (400, 422)
    assert client.get("/api/snapshot?mode=web&mode=mtproto").status_code in (400, 422)
    assert client.get("/styles.css").status_code == 200
