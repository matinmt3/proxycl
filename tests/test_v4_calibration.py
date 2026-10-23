"""Synthetic trials exercise the recorder, never claim real-world evidence."""

import copy
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone

import pytest

from benchmark.calibration import evaluate, record_holdout, record_pair
from config.loader import ScoringConfig
from tests.test_v4_engine import qualified


def observations(stamp, *, policy="v1", network="wifi", latency=30):
    result = qualified(stamp=stamp, latency=latency)
    result.ranking_policy = policy
    return {
        "version": 1,
        "mode": "mtproto",
        "summary": {
            "run_id": f"{stamp}-{policy}",
            "started_at": datetime.fromtimestamp(stamp - 1, timezone.utc).isoformat(),
            "completed_at": datetime.fromtimestamp(stamp + 1, timezone.utc).isoformat(),
            "ranking_policy": policy,
            "scoring_settings": asdict(ScoringConfig(policy=policy)),
            "network": network,
            "stage": "complete",
            "candidate_pool_digest": "a" * 64,
            "selected": 1,
            "verification_profile": "telegram_session",
        },
        "rows": [{**result.to_dict(), "samples": [asdict(sample) for sample in result.samples]}],
    }


def test_empty_calibration_cannot_promote_policy(tmp_path):
    report = evaluate(tmp_path / "trials.json")
    assert not report["evidence_gate_met"]
    assert report["default_policy"] == "v1"
    assert report["complete_pairs"] == 0 and report["missing"]
    assert not (tmp_path / "trials.json").exists()


def test_pair_rejects_different_pool_or_partial_scans(tmp_path):
    now = time.time() - 60
    left, right = observations(now), observations(now + 10, policy="v2-experimental")
    right["summary"]["candidate_pool_digest"] = "b" * 64
    with pytest.raises(ValueError, match="candidate_pool_digest"):
        record_pair(tmp_path / "trials.json", left, right)
    right["summary"]["candidate_pool_digest"] = "a" * 64
    right["summary"]["provisional"] = True
    with pytest.raises(ValueError, match="Partial"):
        record_pair(tmp_path / "trials.json", left, right)


def test_pair_holdout_integrity_and_redaction(tmp_path):
    now = time.time() - 4000
    path = tmp_path / "trials.json"
    left, right = observations(now), observations(now + 10, policy="v2-experimental")
    pair = record_pair(path, left, right, now=now + 15)
    with pytest.raises(ValueError, match="already belongs"):
        record_pair(path, left, right, now=now + 15)
    early = observations(pair["anchor"] + 90)
    with pytest.raises(ValueError, match="window"):
        record_holdout(path, pair["id"], 5, early, now=pair["anchor"] + 100)
    for minutes in (5, 30):
        hold = observations(pair["anchor"] + minutes * 60)
        record_holdout(path, pair["id"], minutes, hold, now=pair["anchor"] + minutes * 60 + 5)
        with pytest.raises(ValueError, match="already recorded"):
            record_holdout(path, pair["id"], minutes, hold, now=pair["anchor"] + minutes * 60 + 5)
    report = evaluate(path)
    assert report["complete_pairs"] == 1 and not report["evidence_gate_met"]
    raw = path.read_text()
    assert "proxy.example" not in raw and "tg://" not in raw and "secret" not in raw


def test_holdout_retests_all_top_candidates_and_requires_finished_connections(tmp_path):
    now = time.time() - 4000
    path = tmp_path / "trials.json"
    pair = record_pair(
        path, observations(now), observations(now + 10, policy="v2-experimental"), now=now + 15
    )
    hold = observations(pair["anchor"] + 300)
    hold["rows"] = []
    with pytest.raises(ValueError, match="every candidate"):
        record_holdout(path, pair["id"], 5, hold, now=pair["anchor"] + 305)
    hold = observations(pair["anchor"] + 300)
    hold["rows"][0]["samples"] = hold["rows"][0]["samples"][:1]
    with pytest.raises(ValueError, match="three finished"):
        record_holdout(path, pair["id"], 5, hold, now=pair["anchor"] + 305)


def test_no_promotion_for_18_pairs_without_three_days_and_two_networks(tmp_path):
    now = time.time() - 36000
    path = tmp_path / "trials.json"
    for index in range(18):
        stamp = now + index * 101
        pair = record_pair(
            path, observations(stamp), observations(stamp + 10, policy="v2-experimental"), now=stamp + 15
        )
        for minutes in (5, 30):
            hold = observations(pair["anchor"] + minutes * 60)
            record_holdout(path, pair["id"], minutes, hold, now=pair["anchor"] + minutes * 60 + 5)
    report = evaluate(path)
    assert report["complete_pairs"] == 18 and not report["evidence_gate_met"]
    assert any("three UTC" in reason for reason in report["missing"])
    assert any("two user" in reason for reason in report["missing"])


def test_complete_fixture_gate_is_only_report_and_does_not_change_default(tmp_path):
    now = time.time() - 5 * 86400
    path = tmp_path / "trials.json"
    for day in range(3):
        for network in ("wifi", "mobile"):
            for repetition in range(3):
                stamp = now + day * 86400 + (5000 if network == "mobile" else 0) + repetition * 100
                pair = record_pair(
                    path,
                    observations(stamp, network=network),
                    observations(stamp + 10, policy="v2-experimental", network=network),
                    now=stamp + 15,
                )
                for minutes in (5, 30):
                    hold = observations(pair["anchor"] + minutes * 60, network=network)
                    record_holdout(path, pair["id"], minutes, hold, now=pair["anchor"] + minutes * 60 + 5)
    report = evaluate(path)
    assert report["evidence_gate_met"] and report["complete_pairs"] == 18
    assert report["default_policy"] == "v1" and not report["automatic_promotion"]
    changed = copy.deepcopy(json.loads(path.read_text()))
    first = changed["pairs"][0]
    first["top"]["v2-experimental"] = ["b" * 64]
    for hold in first["holdouts"].values():
        hold["outcomes"]["b" * 64] = False
    path.write_text(json.dumps(changed))
    assert not evaluate(path)["evidence_gate_met"]


def test_malformed_ledger_never_claims_qualified_evidence(tmp_path):
    path = tmp_path / "trials.json"
    path.write_text(json.dumps({"schema": 1, "pairs": [{"holdouts": {"5": {}, "30": {}}}]}))
    with pytest.raises(ValueError, match="Invalid"):
        evaluate(path)
