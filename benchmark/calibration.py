"""Offline, auditable paired ranking trials; never silently promote a policy.

Only endpoint digests, policy metadata and measured outcomes enter the ledger.
Pair files are full observations.json exports, not self-reported success flags.
The evaluator reports missing real-world evidence; fixtures cannot establish it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from benchmark.scorer import rank, score_all
from config.loader import ScoringConfig
from utils.process_lock import OutputRootLock
from utils.scan_state import _pairs, deserialize_result, write_json_atomic

MAX_BYTES = 16 * 1024 * 1024


def _read(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("Calibration input exceeds 16 MiB")
    return json.loads(raw, object_pairs_hook=_pairs)


def _stamp(value):
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Timezone required")
        return stamp.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError) as exc:
        raise ValueError("Invalid calibration timestamp") from exc


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _observations(document, *, now):
    if not isinstance(document, dict) or document.get("mode") not in {"mtproto", "telegram", "webproxy"}:
        raise ValueError("Calibration requires Telegram observations")
    summary, rows = document.get("summary"), document.get("rows")
    if not isinstance(summary, dict) or not isinstance(rows, list) or len(rows) > 100_000:
        raise ValueError("Invalid observations")
    if summary.get("interrupted") or summary.get("provisional") or summary.get("coverage_limited"):
        raise ValueError("Partial scans cannot establish calibration evidence")
    if summary.get("stage") != "complete":
        raise ValueError("Calibration requires completed scans")
    policy = summary.get("ranking_policy")
    if policy not in {"v1", "v2-experimental"}:
        raise ValueError("Explicit ranking policy required")
    settings = summary.get("scoring_settings")
    if not isinstance(settings, dict) or settings.get("policy") != policy:
        raise ValueError("Scoring settings matching the policy are required")
    try:
        scoring = ScoringConfig(**settings)
    except TypeError as exc:
        raise ValueError("Invalid scoring settings") from exc
    for key, value in vars(scoring).items():
        if key != "policy" and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
            raise ValueError("Invalid scoring weight")
    if (
        scoring.max_latency_ms <= 0
        or sum(
            (
                scoring.telegram_reliability_weight,
                scoring.telegram_latency_weight,
                scoring.telegram_stability_weight,
            )
        )
        <= 0
    ):
        raise ValueError("Scoring settings have no usable weights")
    network = summary.get("network")
    if not isinstance(network, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", network) is None:
        raise ValueError("Invalid network label")
    pool = summary.get("candidate_pool_digest")
    if not isinstance(pool, str) or re.fullmatch(r"[a-f0-9]{64}", pool) is None:
        raise ValueError("Candidate pool digest required; use current-version full observations")
    started, completed = _stamp(summary.get("started_at")), _stamp(summary.get("completed_at"))
    if not started <= completed <= now + 60:
        raise ValueError("Invalid observation chronology")
    results, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid observation row")
        if row.get("ranking_policy", policy) != policy:
            raise ValueError("Row ranking policy differs from scan summary")
        kind = "webproxy" if document["mode"] == "webproxy" else "mtproto"
        result = deserialize_result(
            {
                "proxy": {**row, "kind": kind},
                "samples": row.get("samples"),
                "verification_profile": row.get("verification_profile"),
                "quality_policy": row.get("strict_policy", {}),
                "ranking_policy": row.get("ranking_policy", policy),
                "proof_max_age_seconds": summary.get("proof_max_age_seconds", 1800.0),
            }
        )
        if result.proxy.key() in seen:
            raise ValueError("Duplicate observation candidate")
        seen.add(result.proxy.key())
        if not result.evidence_fresh(now=now):
            raise ValueError("Observation evidence is stale; record promptly after each run")
        if any(not started - 60 <= sample.timestamp <= completed + 60 for sample in result.samples):
            raise ValueError("Sample timestamp does not belong to the supplied run")
        results.append(result)
    return summary, results, started, completed


def _load_ledger(path):
    if not Path(path).exists():
        return {"schema": 1, "policy": "v2-experimental", "pairs": []}
    data = _read(path)
    if not isinstance(data, dict) or data.get("schema") != 1 or not isinstance(data.get("pairs"), list):
        raise ValueError("Invalid calibration ledger")
    if len(data["pairs"]) > 2048:
        raise ValueError("Calibration ledger exceeds 2048 pairs")
    ids, input_digests = set(), set()
    for row in data["pairs"]:
        try:
            if not isinstance(row, dict) or set(row) != {
                "id",
                "network",
                "mode",
                "pool",
                "started_at",
                "anchor",
                "date",
                "run_ids",
                "top",
                "input_digests",
                "holdouts",
            }:
                raise ValueError("Invalid calibration pair fields")
            if not isinstance(row["id"], str) or len(row["id"]) > 128 or row["id"] in ids:
                raise ValueError("Invalid or duplicated pair identity")
            ids.add(row["id"])
            if not isinstance(row["network"], str) or not re.fullmatch(
                r"[A-Za-z0-9_-]{1,32}", row["network"]
            ):
                raise ValueError("Invalid pair network")
            if row["mode"] not in {"mtproto", "telegram", "webproxy"}:
                raise ValueError("Invalid pair mode")
            if any(
                type(row[name]) not in (int, float) or not math.isfinite(row[name]) or row[name] <= 0
                for name in ("started_at", "anchor")
            ):
                raise ValueError("Invalid pair time")
            if (
                row["started_at"] > row["anchor"]
                or row["anchor"] > time.time() + 60
                or row["date"] != datetime.fromtimestamp(row["anchor"], timezone.utc).date().isoformat()
            ):
                raise ValueError("Invalid pair chronology")
            if not isinstance(row["top"], dict) or set(row["top"]) != {"v1", "v2-experimental"}:
                raise ValueError("Invalid pair rankings")
            digests = [row["pool"]]
            for name in ("run_ids", "input_digests"):
                if not isinstance(row[name], list) or len(row[name]) != 2:
                    raise ValueError("Invalid pair provenance")
                digests.extend(row[name])
            for top in row["top"].values():
                if not isinstance(top, list) or not 1 <= len(top) <= 10 or len(set(top)) != len(top):
                    raise ValueError("Invalid top-ten membership")
                digests.extend(top)
            if any(
                not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in digests
            ):
                raise ValueError("Invalid calibration identity digest")
            needed = set(row["top"]["v1"]) | set(row["top"]["v2-experimental"])
            if not isinstance(row["holdouts"], dict) or set(row["holdouts"]) - {"5", "30"}:
                raise ValueError("Invalid holdout interval")
            for minutes, hold in row["holdouts"].items():
                if not isinstance(hold, dict) or set(hold) != {
                    "started_at",
                    "completed_at",
                    "input_digest",
                    "outcomes",
                }:
                    raise ValueError("Invalid holdout fields")
                if (
                    not isinstance(hold["outcomes"], dict)
                    or set(hold["outcomes"]) != needed
                    or any(type(value) is not bool for value in hold["outcomes"].values())
                ):
                    raise ValueError("Invalid holdout outcomes")
                if any(
                    type(hold[name]) not in (int, float) or not math.isfinite(hold[name])
                    for name in ("started_at", "completed_at")
                ):
                    raise ValueError("Invalid holdout time")
                target, slack = row["anchor"] + int(minutes) * 60, 60 if minutes == "5" else 120
                if (
                    not target - slack <= hold["started_at"] <= target + slack
                    or not hold["started_at"] <= hold["completed_at"] <= time.time() + 60
                ):
                    raise ValueError("Invalid holdout chronology")
                if not isinstance(hold["input_digest"], str) or not re.fullmatch(
                    r"[a-f0-9]{64}", hold["input_digest"]
                ):
                    raise ValueError("Invalid holdout provenance")
            used = row["input_digests"] + [hold["input_digest"] for hold in row["holdouts"].values()]
            if len(set(used)) != len(used) or set(used) & input_digests:
                raise ValueError("Calibration observations were reused")
            input_digests.update(used)
        except (KeyError, TypeError, OverflowError) as exc:
            raise ValueError("Malformed calibration ledger") from exc
    return data


def record_pair(path, baseline, candidate, *, now=None):
    """Record contemporaneous v1/v2 runs using the same selected candidate pool."""
    now = time.time() if now is None else now
    left, lresults, lstart, lend = _observations(baseline, now=now)
    right, rresults, rstart, rend = _observations(candidate, now=now)
    if (left["ranking_policy"], right["ranking_policy"]) != ("v1", "v2-experimental"):
        raise ValueError("A pair must contain v1 then v2-experimental observations")
    for field in ("network", "candidate_pool_digest", "selected", "verification_profile"):
        if left.get(field) != right.get(field):
            raise ValueError(f"Paired scans differ in {field}")
    if {k: v for k, v in left["scoring_settings"].items() if k != "policy"} != {
        k: v for k, v in right["scoring_settings"].items() if k != "policy"
    }:
        raise ValueError("Paired scans must use equal scoring weights")
    if baseline["mode"] != candidate["mode"]:
        raise ValueError("Paired modes differ")
    if abs(lend - rend) > 1800:
        raise ValueError("Paired scans must finish within 30 minutes")
    tops = {}
    for name, results in (("v1", lresults), ("v2-experimental", rresults)):
        settings = left["scoring_settings"] if name == "v1" else right["scoring_settings"]
        ranked = rank(score_all([r for r in results if r.quality_eligible], ScoringConfig(**settings)))
        tops[name] = [_digest(result.proxy.key()) for result in ranked[:10]]
    if not all(tops.values()):
        raise ValueError("Both policies need at least one qualified candidate")
    row = {
        "id": str(uuid4()),
        "network": left["network"],
        "mode": baseline["mode"],
        "pool": left["candidate_pool_digest"],
        "started_at": min(lstart, rstart),
        "anchor": max(lend, rend),
        "date": datetime.fromtimestamp(max(lend, rend), timezone.utc).date().isoformat(),
        "run_ids": [_digest(str(left.get("run_id"))), _digest(str(right.get("run_id")))],
        "top": tops,
        "input_digests": [_digest(json.dumps(doc, sort_keys=True)) for doc in (baseline, candidate)],
        "holdouts": {},
    }
    with OutputRootLock(Path(path).parent, operation="calibration"):
        ledger = _load_ledger(path)
        if len(ledger["pairs"]) >= 2048:
            raise ValueError("Calibration ledger is full; archive it before recording more trials")
        if any(set(pair["input_digests"]) & set(row["input_digests"]) for pair in ledger["pairs"]):
            raise ValueError("Observation already belongs to a recorded pair")
        ledger["pairs"].append(row)
        write_json_atomic(Path(path), ledger)
    return row


def record_holdout(path, pair_id, minutes, observations, *, now=None):
    """Attach independent measured outcomes around +5m (±1m) / +30m (±2m)."""
    if minutes not in (5, 30):
        raise ValueError("Holdout interval must be 5 or 30 minutes")
    now = time.time() if now is None else now
    summary, results, started, completed = _observations(observations, now=now)
    with OutputRootLock(Path(path).parent, operation="calibration"):
        ledger = _load_ledger(path)
        row = next((row for row in ledger["pairs"] if row["id"] == pair_id), None)
        if row is None:
            raise ValueError("Unknown pair ID")
        if str(minutes) in row["holdouts"]:
            raise ValueError("Holdout already recorded; observations cannot overwrite a trial")
        input_digest = _digest(json.dumps(observations, sort_keys=True))
        if any(
            input_digest == hold.get("input_digest")
            for pair in ledger["pairs"]
            for hold in pair["holdouts"].values()
        ):
            raise ValueError("Holdout observations cannot be reused in another trial")
        if (summary["network"], observations["mode"]) != (row["network"], row["mode"]):
            raise ValueError("Holdout network/mode differs")
        target, slack = row["anchor"] + minutes * 60, 60 if minutes == 5 else 120
        if not target - slack <= started <= target + slack:
            raise ValueError("Holdout start is outside its required observation window")
        observations_by_id = {_digest(result.proxy.key()): result for result in results}
        needed = set(row["top"]["v1"]) | set(row["top"]["v2-experimental"])
        if not needed <= observations_by_id.keys():
            raise ValueError("Holdout must independently retest every candidate in both top tens")
        outcomes = {}
        for key in needed:
            result = observations_by_id[key]
            if result.attempts < 3:
                raise ValueError("Holdout requires three finished connections per candidate")
            outcomes[key] = result.quality_eligible
        row["holdouts"][str(minutes)] = {
            "started_at": started,
            "completed_at": completed,
            "input_digest": input_digest,
            "outcomes": outcomes,
        }
        write_json_atomic(Path(path), ledger)
    return row


def evaluate(path):
    """Report the evidence gate without changing the shipped default policy."""
    ledger = _load_ledger(path)
    complete = [row for row in ledger["pairs"] if set(row.get("holdouts", {})) == {"5", "30"}]
    days = {row["date"] for row in complete}
    networks = {row["network"] for row in complete}
    missing = []
    if len(complete) < 18:
        missing.append(f"Need {18 - len(complete)} more complete paired trials")
    if len(days) < 3:
        missing.append("Need trials across at least three UTC dates")
    if len(networks) < 2:
        missing.append("Need trials on at least two user-identified networks")
    comparisons = []
    for minutes in (5, 30):
        for size in (1, 10):
            deltas = []
            for row in complete:
                scores = []
                for policy in ("v1", "v2-experimental"):
                    ids = row["top"][policy][:size]
                    scores.append(
                        sum(row["holdouts"][str(minutes)]["outcomes"][key] for key in ids) / len(ids)
                    )
                deltas.append(scores[1] - scores[0])
            mean = sum(deltas) / len(deltas) if deltas else None
            # A paired normal interval is diagnostic, not a proof of universal
            # superiority. All-zero difference passes no-regression, not gain.
            spread = (
                (sum((value - mean) ** 2 for value in deltas) / (len(deltas) - 1)) ** 0.5
                if len(deltas) > 1
                else None
            )
            lower = mean - 1.96 * spread / math.sqrt(len(deltas)) if spread is not None else None
            comparisons.append(
                {
                    "minutes": minutes,
                    "top": size,
                    "pairs": len(deltas),
                    "mean_delta": mean,
                    "lower_95_approx": lower,
                }
            )
            if lower is None or lower < 0:
                missing.append(f"No reproducible non-regression for top{size} at +{minutes}m")
    return {
        "schema": 1,
        "complete_pairs": len(complete),
        "pending_pairs": len(ledger["pairs"]) - len(complete),
        "days": sorted(days),
        "networks": sorted(networks),
        "comparisons": comparisons,
        "evidence_gate_met": not missing,
        "missing": missing,
        "default_policy": "v1",
        "automatic_promotion": False,
        "caveat": "Local ledger evidence is not independently attested; review real-device/network provenance before promotion.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("record", "holdout", "evaluate"))
    parser.add_argument("--file", required=True)
    parser.add_argument("--baseline")
    parser.add_argument("--candidate")
    parser.add_argument("--pair-id")
    parser.add_argument("--minutes", type=int, choices=(5, 30))
    parser.add_argument("--observations")
    args = parser.parse_args(argv)
    try:
        if args.action == "record":
            if not args.baseline or not args.candidate:
                parser.error("record needs --baseline and --candidate observations.json files")
            result = record_pair(args.file, _read(args.baseline), _read(args.candidate))
        elif args.action == "holdout":
            if not args.pair_id or not args.minutes or not args.observations:
                parser.error("holdout needs --pair-id, --minutes and --observations")
            result = record_holdout(args.file, args.pair_id, args.minutes, _read(args.observations))
        else:
            result = evaluate(args.file)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
