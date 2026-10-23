"""Dashboard must not upgrade transport-only or inconsistent evidence into strict results."""

import copy
import json
import time

import pytest

from dashboard import data


def strict_row():
    observed = time.time() - 5
    return {
        "server": "session.example",
        "port": 443,
        "secret": "00112233445566778899aabbccddeeff",
        "source": "fixture",
        "oldest_observed_at": observed,
        "latest_observed_at": observed + 1,
        "proof_max_age_seconds": 3600,
        "evidence_expires_at": observed + 3600,
        "tg_link": "tg://proxy?server=session.example&port=443&secret=00112233445566778899aabbccddeeff",
        "web_link": "https://t.me/proxy?server=session.example&port=443&secret=00112233445566778899aabbccddeeff",
        "score": 0.8,
        "success_rate": 1.0,
        "attempts": 3,
        "successes": 3,
        "avg_latency_ms": 2000,
        "stability": 0.9,
        "verification_profile": "telegram_session",
        "strict_eligible": True,
        "strict_successes": 3,
        "dc_ids_verified": [2, 4],
        "ping_count": 9,
        "telegram_median_ms": 100,
        "telegram_p95_ms": 120,
        "telegram_jitter_ms": 5,
        "reliability_lower_bound": 0.4385,
        "strict_policy": {
            "min_successes": 3,
            "min_success_rate": 0.8,
            "required_dc_ids": [2, 4],
            "min_ping_count": 3,
            "max_p95_ms": 1500,
            "max_jitter_ms": 500,
        },
    }


def save(root, rows, profile="telegram_session", mode="mtproto"):
    folder = root / mode
    folder.mkdir(exist_ok=True)
    summary = {
        "started_at": "2026-10-21T12:00:00+00:00",
        "completed_at": "2026-10-21T12:01:00+00:00",
        "requested_count": None,
        "collected": len(rows),
        "selected": len(rows),
        "tested": len(rows),
        "verified": len(rows),
        "eligible": len(rows),
        "displayed": min(10, len(rows)),
        "failure_counts": {},
    }
    if profile is not None:
        summary.update(
            verification_profile=profile,
            transport_reachable=len(rows),
            strict_verified=len(rows) if profile == "telegram_session" else 0,
        )
    body = {"version": 1, "mode": mode, "summary": summary, "source_reports": [], "rows": rows}
    (folder / "snapshot.json").write_text(json.dumps(body), encoding="utf-8")


def test_old_snapshot_is_explicit_transport_evidence_without_usable_actions(tmp_path):
    row = strict_row()
    for key in list(row):
        if key.startswith(("strict_", "telegram_")) or key in {
            "verification_profile",
            "dc_ids_verified",
            "ping_count",
            "reliability_lower_bound",
        }:
            del row[key]
    save(tmp_path, [row], None)
    current = data.snapshot(tmp_path)
    assert current["state"] == "ready"
    assert current["summary"]["verification_profile"] == "transport"
    assert current["summary"]["strict_verified"] == 0
    assert "transport" in current["message"].lower()
    assert current["rows"][0]["tg_link"] is None
    assert current["rows"][0]["strict_eligible"] is False


def test_strong_session_evidence_keeps_rtt_metrics_and_safe_links(tmp_path):
    row = strict_row()
    save(tmp_path, [row])
    current = data.snapshot(tmp_path)
    assert current["state"] == "ready"
    assert current["rows"][0]["telegram_p95_ms"] == 120
    assert current["rows"][0]["tg_link"] == row["tg_link"]
    assert current["summary"]["strict_verified"] == 1


@pytest.mark.parametrize(
    "change",
    [
        {"verification_profile": "transport"},
        {"strict_eligible": False},
        {"strict_eligible": "true"},
        {"strict_successes": 1, "attempts": 1, "successes": 1, "ping_count": 3},
        {"strict_successes": True},
        {"dc_ids_verified": [2]},
        {"dc_ids_verified": [2, True]},
        {"ping_count": 8},
        {"telegram_median_ms": 121},
        {"telegram_p95_ms": 1501},
        {"telegram_jitter_ms": 501},
        {"reliability_lower_bound": 1.1},
        {"reliability_lower_bound": 0.9},
        {"success_rate": 0.5},
        {"dc_ids_verified": [2, 4, 2]},
        {"attempts": 4, "success_rate": 0.75},
    ],
)
def test_inconsistent_strict_evidence_fails_closed(tmp_path, change):
    row = strict_row()
    row.update(change)
    save(tmp_path, [row])
    current = data.snapshot(tmp_path)
    assert current["state"] == "corrupt" and current["rows"] == []


@pytest.mark.parametrize("field", ["strict_policy", "ping_count", "telegram_p95_ms", "verification_profile"])
def test_snapshot_profile_alone_cannot_claim_strict(tmp_path, field):
    row = strict_row()
    del row[field]
    save(tmp_path, [row])
    assert data.snapshot(tmp_path)["state"] == "corrupt"


@pytest.mark.parametrize(
    "policy_change",
    [
        {"min_successes": 1},
        {"min_success_rate": 0.1},
        {"required_dc_ids": [2]},
        {"required_dc_ids": [2, 4, 2]},
        {"min_ping_count": 1},
        {"max_jitter_ms": 0},
    ],
)
def test_weak_policy_cannot_promote_a_diagnostic_result(tmp_path, policy_change):
    row = strict_row()
    row["strict_policy"].update(policy_change)
    save(tmp_path, [row])
    assert data.snapshot(tmp_path)["state"] == "corrupt"


def test_dashboard_obeys_stronger_saved_policy_and_sorts_by_telegram_rtt(tmp_path):
    row = strict_row()
    row["strict_policy"]["max_p95_ms"] = 110
    save(tmp_path, [row])
    assert data.snapshot(tmp_path)["state"] == "corrupt"
    first = strict_row()
    second = copy.deepcopy(first)
    second.update(server="slower.example", avg_latency_ms=10, telegram_p95_ms=400)
    save(tmp_path, [first, second])
    rows = data.proxies(tmp_path, sort_by="telegram_p95_ms", order="asc")
    assert [r["server"] for r in rows] == ["session.example", "slower.example"]


def test_http_mode_never_exposes_telegram_proof_or_accepts_telegram_rtt_sort(tmp_path):
    row = strict_row()
    row.update(protocol="http", uri="http://session.example:443")
    save(tmp_path, [row], "web", "web")
    current = data.snapshot(tmp_path, "web")
    assert current["state"] == "ready"
    public = current["rows"][0]
    assert public["uri"] == "http://session.example:443"
    assert not any(key.startswith(("strict_", "telegram_")) for key in public)
    assert "dc_ids_verified" not in public and "ping_count" not in public
    with pytest.raises(ValueError):
        data.proxies(tmp_path, mode="web", sort_by="telegram_p95_ms")
