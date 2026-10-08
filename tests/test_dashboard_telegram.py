import base64
import json
import threading
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from dashboard import data, server
from tests.test_dashboard_v3 import write_snapshot

SECRET = "00112233445566778899aabbccddeeff"


def telegram_row():
    query = urlencode({"server": "telegram-share.example", "port": 443, "secret": SECRET})
    return {
        "server": "telegram-share.example",
        "port": 443,
        "secret": SECRET,
        "score": 0.8,
        "success_rate": 1,
        "avg_latency_ms": 20,
        "tg_link": "tg://proxy?" + query,
        "web_link": "https://t.me/proxy?" + query,
    }


def test_telegram_snapshot_is_isolated_and_retains_both_safe_share_forms(tmp_path):
    row = telegram_row()
    write_snapshot(tmp_path, "telegram", [row])
    (tmp_path / "proxy.json").write_text(json.dumps([row]), encoding="utf-8")
    current = data.snapshot(tmp_path, "telegram")
    assert current["state"] == "ready" and current["mode"] == "telegram"
    assert current["rows"][0]["web_link"] == row["web_link"]
    assert current["rows"][0]["tg_link"] == row["tg_link"]
    assert "protocol" not in current["rows"][0] and "uri" not in current["rows"][0]
    assert data.snapshot(tmp_path, "web")["state"] == "missing"
    (tmp_path / "telegram" / "snapshot.json").unlink()
    assert data.snapshot(tmp_path, "telegram")["state"] == "missing"


@pytest.mark.parametrize(
    "link",
    [
        "http://t.me/proxy?server=telegram-share.example&port=443&secret=" + SECRET,
        "https://t.me.evil.example/proxy?server=telegram-share.example&port=443&secret=" + SECRET,
        "https://t.me:8443/proxy?server=telegram-share.example&port=443&secret=" + SECRET,
        "https://user@t.me/proxy?server=telegram-share.example&port=443&secret=" + SECRET,
        "https://t.me/other?server=telegram-share.example&port=443&secret=" + SECRET,
        "https://t.me/proxy?server=other.example&port=443&secret=" + SECRET,
        "https://t.me/proxy?server=telegram-share.example&port=80&secret=" + SECRET,
        "https://t.me/proxy?server=telegram-share.example&port=443&secret=" + SECRET + "&secret=",
        "https://t.me/proxy?server=telegram-share.example&port=443&secret=abcdefghijklmnop",
    ],
)
def test_telegram_action_requires_fixed_https_origin_matching_endpoint_and_valid_secret(tmp_path, link):
    row = telegram_row()
    row["web_link"] = link
    write_snapshot(tmp_path, "telegram", [row])
    assert data.snapshot(tmp_path, "telegram")["rows"][0]["web_link"] is None


@pytest.mark.parametrize(
    "secret",
    [
        SECRET,
        "dd" + SECRET,
        "ee" + SECRET + "example.org".encode().hex(),
        base64.urlsafe_b64encode(bytes.fromhex(SECRET)).decode().rstrip("="),
    ],
)
def test_telegram_share_action_accepts_all_verified_secret_forms_and_ipv6(tmp_path, secret):
    row = telegram_row()
    row.update(server="2001:db8::1", secret=secret)
    query = urlencode({"server": row["server"], "port": row["port"], "secret": secret})
    row.update(tg_link="tg://proxy?" + query, web_link="https://t.me/proxy?" + query)
    write_snapshot(tmp_path, "telegram", [row])
    assert data.snapshot(tmp_path, "telegram")["rows"][0]["web_link"] == row["web_link"]


def test_matching_but_malformed_secret_cannot_be_used_as_a_share_action(tmp_path):
    row = telegram_row()
    row["secret"] = "abcdefghijklmnop"
    row["web_link"] = "https://t.me/proxy?" + urlencode(
        {"server": row["server"], "port": row["port"], "secret": row["secret"]}
    )
    write_snapshot(tmp_path, "telegram", [row])
    assert data.snapshot(tmp_path, "telegram")["rows"][0]["web_link"] is None


@pytest.mark.parametrize("secret", [None, False, 0, "", "malformed-secret"])
def test_telegram_share_action_requires_a_valid_secret_on_the_verified_row(tmp_path, secret):
    row = telegram_row()
    row["secret"] = secret
    write_snapshot(tmp_path, "telegram", [row])
    assert data.snapshot(tmp_path, "telegram")["rows"][0]["web_link"] is None
    row.pop("secret")
    write_snapshot(tmp_path, "telegram", [row])
    current = data.snapshot(tmp_path, "telegram")["rows"][0]
    assert current["web_link"] is None
    assert current["tg_link"] is None


def test_telegram_share_secret_equality_is_semantic_not_encoding_based(tmp_path):
    row = telegram_row()
    row["secret"] = base64.urlsafe_b64encode(bytes.fromhex(SECRET)).decode().rstrip("=")
    write_snapshot(tmp_path, "telegram", [row])
    assert data.snapshot(tmp_path, "telegram")["rows"][0]["web_link"] == row["web_link"]
    row["secret"] = "dd" + SECRET  # Same AES key but a different transport secret.
    write_snapshot(tmp_path, "telegram", [row])
    assert data.snapshot(tmp_path, "telegram")["rows"][0]["web_link"] is None


def test_http_proxy_rows_never_keep_telegram_share_links_or_secrets(tmp_path):
    row = telegram_row()
    row.update(protocol="http", uri="http://telegram-share.example:443")
    write_snapshot(tmp_path, "web", [row])
    current = data.snapshot(tmp_path, "web")["rows"][0]
    assert "web_link" not in current and "tg_link" not in current and "secret" not in current
    assert current["uri"] == row["uri"]


def test_interrupted_snapshot_has_accurate_skipped_counts_without_marking_all_completed(tmp_path):
    body = write_snapshot(tmp_path, "telegram", [telegram_row()])
    body["summary"].update(interrupted=True, selected=8, tested=3, skipped=5)
    path = tmp_path / "telegram" / "snapshot.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    current = data.snapshot(tmp_path, "telegram")
    assert current["state"] == "ready"
    assert current["summary"]["interrupted"] is True and current["summary"]["skipped"] == 5
    assert "partial" in current["message"].lower() and "completed" not in current["message"].lower()
    with pytest.raises(ValueError):
        data.proxies(tmp_path, mode="telegram", protocol="socks5")


@pytest.mark.parametrize(
    "change",
    [
        {"interrupted": "true"},
        {"skipped": -1},
        {"skipped": 4},
        {"skipped": True},
        {"tested": 9},
        {"interrupted": False},
    ],
)
def test_invalid_interrupted_metadata_is_corrupt(tmp_path, change):
    body = write_snapshot(tmp_path, "telegram", [telegram_row()])
    body["summary"].update(interrupted=True, selected=8, tested=3, skipped=5)
    body["summary"].update(change)
    (tmp_path / "telegram" / "snapshot.json").write_text(json.dumps(body), encoding="utf-8")
    assert data.snapshot(tmp_path, "telegram")["state"] == "corrupt"


@pytest.mark.parametrize(
    "change",
    [
        {"retry_count": 0},
        {"retry_count": True},
        {"retry_count": 1.5},
        {"fully_tested": -1},
        {"fully_tested": True},
        {"partial_tested": 99},
        {"partial_tested": -1},
        {"partial_tested": 1.5},
        {"fully_tested": 2},
        {"retry_count": 1},
    ],
)
def test_retry_metadata_must_be_valid_and_partition_tested_candidates(tmp_path, change):
    body = write_snapshot(tmp_path, "telegram", [telegram_row()])
    body["summary"].update(
        interrupted=True, selected=8, tested=3, skipped=5, retry_count=3, fully_tested=1, partial_tested=2
    )
    body["summary"].update(change)
    (tmp_path / "telegram" / "snapshot.json").write_text(json.dumps(body), encoding="utf-8")
    assert data.snapshot(tmp_path, "telegram")["state"] == "corrupt"


def test_valid_retry_metadata_counts_all_tested_candidates_even_if_rows_are_filtered(tmp_path):
    body = write_snapshot(tmp_path, "telegram", [telegram_row()])
    body["summary"].update(
        interrupted=True, selected=8, tested=3, skipped=5, retry_count=3, fully_tested=1, partial_tested=2
    )
    (tmp_path / "telegram" / "snapshot.json").write_text(json.dumps(body), encoding="utf-8")
    current = data.snapshot(tmp_path, "telegram")
    assert current["state"] == "ready" and len(current["rows"]) == 1
    assert current["summary"]["fully_tested"] + current["summary"]["partial_tested"] == 3


def test_both_http_adapters_expose_telegram_mode(tmp_path, monkeypatch):
    write_snapshot(tmp_path, "telegram", [telegram_row()])
    httpd = server.create_server("127.0.0.1", 0, tmp_path)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    try:
        with urlopen(f"http://127.0.0.1:{httpd.server_port}/api/snapshot?mode=telegram", timeout=3) as reply:
            assert json.load(reply)["rows"][0]["web_link"] == telegram_row()["web_link"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from dashboard import app as adapter

    monkeypatch.setattr(adapter, "OUTPUT_FOLDER", tmp_path)
    client = TestClient(adapter.app)
    assert client.get("/api/snapshot?mode=telegram").json() == data.snapshot(tmp_path, "telegram")
    assert client.get("/api/proxies?mode=telegram").json()[0]["web_link"] == telegram_row()["web_link"]
    assert client.get("/api/proxies?mode=telegram&protocol=socks5").status_code == 422
