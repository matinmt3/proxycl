"""Progress and history are isolated observations, never recommendation proof."""

import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from dashboard import data
from tests.test_dashboard_v3 import write_snapshot
from utils.process_lock import OutputRootLock


def progress(**extra):
    return {
        "version": 1,
        "mode": "mtproto",
        "network": "default",
        "run_id": "run-1",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "stage": "finalists",
        "completed": 2,
        "total": 20,
        "qualified": 1,
        "remaining_seconds": 60.5,
        "active": True,
        "resumable": True,
        **extra,
    }


def history_row(**extra):
    return {
        "identity": "a" * 64,
        "server": "proxy.example",
        "port": 443,
        "kind": "mtproto",
        "protocol": "mtproto",
        "network": "default",
        "mode": "mtproto",
        "last_tested": time.time(),
        "successes": 7,
        "attempts": 8,
        "qualified": True,
        "p95_ms": 110,
        "current_run_id": "run-1",
        "run_count": 2,
        "historical_attempts": 16,
        "historical_successes": 14,
        **extra,
    }


def extra_file(tmp_path, name, row, mode="mtproto"):
    (tmp_path / mode).mkdir(parents=True, exist_ok=True)
    (tmp_path / mode / name).write_text(json.dumps(row), encoding="utf-8")


def api(tmp_path, mode="mtproto"):
    return data.api_response("/api/snapshot", tmp_path, {"mode": [mode]})


def test_live_progress_survives_missing_snapshot_without_promoting_proxies(tmp_path):
    extra_file(tmp_path, "progress.json", progress(auth_key="DO NOT EXPOSE"))
    with OutputRootLock(tmp_path):
        current = api(tmp_path)
    assert current["state"] == "missing"
    assert current["rows"] == []
    assert current["progress"]["stage"] == "finalists"
    assert "auth_key" not in current["progress"]
    assert current["history"] == []
    assert current["summary"] == {}


def test_single_stage_http_scan_keeps_its_live_testing_progress(tmp_path):
    extra_file(tmp_path, "progress.json", progress(mode="web", stage="testing"), mode="web")
    with OutputRootLock(tmp_path):
        current = api(tmp_path, "web")
    assert current["progress_state"] == "ready"
    assert current["progress"]["stage"] == "testing" and current["progress"]["active"]


@pytest.mark.parametrize(
    "change",
    [
        {"network": "mobile"},
        {"mode": "web"},
        {"version": True},
        {"completed": -1},
        {"completed": 21},
        {"qualified": True},
        {"remaining_seconds": float("nan")},
        {"active": "yes"},
        {"stage": "authorized"},
        {"updated_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()},
    ],
)
def test_bad_progress_is_ignored_without_hiding_completed_results(tmp_path, change):
    write_snapshot(tmp_path, "mtproto", [])
    extra_file(tmp_path, "progress.json", progress(**change))
    current = api(tmp_path)
    assert current["state"] == "ready"
    assert current["progress"] is None
    assert current["progress_state"] == "corrupt"


def test_stale_progress_is_not_displayed_as_live(tmp_path):
    extra_file(
        tmp_path,
        "progress.json",
        progress(updated_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()),
    )
    with OutputRootLock(tmp_path):
        current = api(tmp_path)
    assert current["progress"] is None
    assert current["progress_state"] == "stale"

    recovered = api(tmp_path)["progress"]
    assert recovered["recovery_needed"] is True
    assert recovered["active"] is False
    assert recovered["stage"] == "stopped"


def test_stopped_progress_is_past_activity_with_resume_command_preserved(tmp_path):
    extra_file(
        tmp_path,
        "progress.json",
        progress(
            updated_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
            active=False,
            stage="stopped",
            command="recheck",
        ),
    )
    current = api(tmp_path)
    assert current["progress"]["active"] is False
    assert current["progress"]["command"] == "recheck"


@pytest.mark.parametrize("total", [0, 5])
@pytest.mark.parametrize("active", [False, True])
def test_completed_progress_never_claims_resume_or_orphan_recovery(tmp_path, total, active):
    extra_file(
        tmp_path,
        "progress.json",
        progress(stage="complete", active=active, completed=total, total=total, qualified=0),
    )
    current = api(tmp_path)["progress"]
    assert current["stage"] == "complete"
    assert not current["resumable"] and not current["active"] and not current["recovery_needed"]


def test_history_has_no_links_or_private_keys_and_is_not_live_eligibility(tmp_path):
    extra_file(
        tmp_path,
        "history.json",
        {
            "version": 1,
            "mode": "mtproto",
            "network": "default",
            "rows": [history_row(secret="DO NOT EXPOSE", tg_link="tg://proxy", auth_key="bad")],
        },
    )
    current = api(tmp_path)
    assert current["state"] == "missing"
    assert current["rows"] == []
    assert current["history"][0]["historical_attempts"] == 16
    assert not {"secret", "tg_link", "auth_key"} & current["history"][0].keys()


def test_network_folder_and_proxy_modes_are_isolated(tmp_path):
    folder = tmp_path / "networks" / "mobile"
    extra_file(folder, "progress.json", progress(network="mobile"))
    assert api(folder)["progress"]["network"] == "mobile"
    assert api(tmp_path)["progress"] is None
    assert api(folder, "web")["progress"] is None
    extra_file(folder, "progress.json", progress(network="default"))
    assert api(folder)["progress"] is None
    body = write_snapshot(tmp_path, "mtproto", [])
    body["summary"]["network"] = "mobile"
    extra_file(tmp_path, "snapshot.json", body)
    assert api(tmp_path)["state"] == "corrupt"


def test_source_health_optional_fields_are_sanitized(tmp_path):
    body = write_snapshot(tmp_path, "mtproto", [])
    body["source_reports"] = [
        {
            "name": "feed",
            "status": "cooldown",
            "count": 0,
            "checked": 9,
            "qualified": 2,
            "cooldown_until": time.time() + 1800,
            "auth_key": "DO NOT EXPOSE",
        }
    ]
    extra_file(tmp_path, "snapshot.json", body)
    current = api(tmp_path)
    assert current["state"] == "ready"
    assert current["source_reports"][0]["checked"] == 9
    assert current["source_reports"][0]["qualified"] == 2
    assert "auth_key" not in current["source_reports"][0]


def test_provisional_stage_metadata_never_qualifies_missing_proof(tmp_path):
    body = write_snapshot(tmp_path, "mtproto", [])
    body["summary"].update(
        strategy="staged", stage="stopped", network="default", resumed=True, provisional=True
    )
    extra_file(tmp_path, "snapshot.json", body)
    current = api(tmp_path)
    assert current["state"] == "ready"
    assert current["rows"] == []
    assert current["summary"]["provisional"] is True


def test_helper_size_limit_and_corrupt_history_are_local_failures(tmp_path, monkeypatch):
    write_snapshot(tmp_path, "mtproto", [])
    monkeypatch.setattr(data, "AUXILIARY_MAX_BYTES", 100)
    (tmp_path / "mtproto" / "progress.json").write_text(" " * 101)
    (tmp_path / "mtproto" / "history.json").write_text("{bad")
    current = api(tmp_path)
    assert current["state"] == "ready"
    assert current["progress_state"] == current["history_state"] == "corrupt"
