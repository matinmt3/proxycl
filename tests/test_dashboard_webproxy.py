"""Telegram WEB is isolated from both old share links and HTTP/SOCKS."""

import pytest

from dashboard import data
from telegram_webproxy.models import TelegramWebProxy
from tests.test_dashboard_strict import save, strict_row


def web_row(address="relay.example/base/path", secret="00112233445566778899aabbccddeeff"):
    proxy = TelegramWebProxy(address, secret)
    return {
        **strict_row(),
        **proxy.to_dict(),
        "web_link": proxy.web_link(),
        "tg_link": proxy.tg_link(),
    }


@pytest.mark.parametrize("address", ["relay.example", "relay.example/base/path"])
@pytest.mark.parametrize("secret", ["00112233445566778899aabbccddeeff", "dd00112233445566778899aabbccddeeff"])
def test_webproxy_has_matched_direct_and_share_actions_after_strict_proof(tmp_path, address, secret):
    row = web_row(address, secret)
    save(tmp_path, [row], mode="webproxy")
    result = data.snapshot(tmp_path, "webproxy")
    assert result["state"] == "ready"
    assert result["rows"][0]["protocol"] == "webproxy"
    assert result["rows"][0]["web_link"] == row["web_link"]
    assert result["rows"][0]["tg_link"] == row["tg_link"]
    assert data.snapshot(tmp_path, "telegram")["state"] == "missing"
    assert data.snapshot(tmp_path, "mtproto")["state"] == "missing"
    assert data.snapshot(tmp_path, "web")["state"] == "missing"


@pytest.mark.parametrize(
    "replacement",
    [
        "https://t.me/proxy?server=relay.example&port=443&secret=00112233445566778899aabbccddeeff",
        "https://evil.example/webproxy?server=relay.example&secret=00112233445566778899aabbccddeeff",
        "https://t.me/webproxy?server=other.example&secret=00112233445566778899aabbccddeeff",
        "https://t.me/webproxy?server=relay.example/base/path&secret=00112233445566778899aabbccddeeff",
        "https://t.me/webproxy?server=relay.example&port=443&secret=00112233445566778899aabbccddeeff",
        "tg://webproxy?server=relay.example&secret=00112233445566778899aabbccddeeff",
        "javascript:alert(1)",
    ],
)
def test_wrong_endpoint_format_or_unmarked_path_has_no_connection_action(tmp_path, replacement):
    row = web_row()
    row["web_link"] = replacement
    save(tmp_path, [row], mode="webproxy")
    result = data.snapshot(tmp_path, "webproxy")
    assert result["state"] == "ready"
    assert result["rows"][0]["web_link"] is None
    assert result["rows"][0]["tg_link"] is None


def test_webproxy_missing_strict_evidence_is_not_promoted(tmp_path):
    row = web_row()
    row["ping_count"] = 1
    save(tmp_path, [row], mode="webproxy")
    result = data.snapshot(tmp_path, "webproxy")
    assert result["state"] == "corrupt"
    assert result["rows"] == []


def test_network_query_uses_scoped_folder_and_default_preserves_legacy_callers(tmp_path):
    mobile = tmp_path / "networks" / "mobile"
    mobile.mkdir(parents=True)
    save(tmp_path, [web_row("default.example")], mode="webproxy")
    save(mobile, [web_row("mobile.example")], mode="webproxy")
    assert (
        data.api_response("/api/snapshot", tmp_path, {"mode": ["webproxy"], "network": ["mobile"]})["rows"][
            0
        ]["server"]
        == "mobile.example"
    )
    assert (
        data.api_response("/api/snapshot", mobile, {"mode": ["webproxy"]})["rows"][0]["server"]
        == "mobile.example"
    )
    assert (
        data.api_response("/api/snapshot", mobile, {"mode": ["webproxy"], "network": ["default"]})["rows"][0][
            "server"
        ]
        == "default.example"
    )
    for label in ("../mobile", "CON", "a/b", "", "mobile%2f.."):
        with pytest.raises(ValueError):
            data.api_response("/api/snapshot", tmp_path, {"mode": ["webproxy"], "network": [label]})
