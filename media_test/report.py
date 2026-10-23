"""Allowlisted media reports: no account, message URL, proxy secret or exception text."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from pathlib import Path

from media_test.models import MEDIA_POLICY, REPORT_SCHEMA

STATUSES = {
    "complete",
    "partial",
    "cancelled",
    "time_limit",
    "budget_limit",
    "expired_evidence",
    "file_unavailable",
    "file_changed",
    "cdn_unsupported",
    "account_limited",
    "authentication_failed",
    "network_failed",
    "protocol_failed",
    "short_sample",
    "not_tested",
    "internal_error",
}
LOGOUT_STATUSES = {"not_logged_in", "confirmed", "not_confirmed"}


def _number(value, *, default=0):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else default


def _digest(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else ""


def _network(value):
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value) else "default"


def safe_report(document: dict) -> dict:
    if not isinstance(document, dict) or document.get("schema_version") != REPORT_SCHEMA:
        raise ValueError("Unsupported media report")
    rows = []
    for row in document.get("results", [])[:5]:
        if not isinstance(row, dict) or not _digest(row.get("candidate_id")):
            continue
        rounds = []
        for item in row.get("rounds", [])[:2]:
            if not isinstance(item, dict):
                continue
            rounds.append(
                {
                    "status": item.get("status") if item.get("status") in STATUSES else "internal_error",
                    **{
                        key: _number(item.get(key), default=None)
                        for key in ("bytes", "elapsed_seconds", "ttfb_ms", "stalls", "mbps")
                    },
                    "sha256": _digest(item.get("sha256")),
                    "short_sample": bool(item.get("short_sample", True)),
                }
            )
        rows.append(
            {
                "candidate_id": row["candidate_id"],
                "network_profile": _network(row.get("network_profile")),
                "proof_expires_at": _number(row.get("proof_expires_at"), default=None),
                "ranking_policy": (
                    row.get("ranking_policy")
                    if row.get("ranking_policy") in {"v1", "v2-experimental"}
                    else "v1"
                ),
                "status": row.get("status") if row.get("status") in STATUSES else "internal_error",
                "rounds": rounds,
                **{
                    key: _number(row.get(key), default=None)
                    for key in ("completed_rounds", "median_mbps", "median_ttfb_ms", "stalls")
                },
            }
        )
    ids = {row["candidate_id"] for row in rows}
    return {
        "schema_version": REPORT_SCHEMA,
        "policy": MEDIA_POLICY,
        "experimental": True,
        "status": document.get("status") if document.get("status") in STATUSES else "internal_error",
        "created_at": _number(document.get("created_at")),
        "network_profile": _network(document.get("network_profile")),
        **{
            key: _number(document.get(key))
            for key in ("budget_bytes", "observed_bytes", "downloaded_bytes", "elapsed_seconds")
        },
        "logout_status": (
            document.get("logout_status")
            if document.get("logout_status") in LOGOUT_STATUSES
            else "not_confirmed"
        ),
        "results": rows,
        "ranked_candidate_ids": [
            value for value in document.get("ranked_candidate_ids", [])[:5] if value in ids
        ],
    }


def write_report(path, document: dict) -> None:
    path = Path(path)
    payload = json.dumps(safe_report(document), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".media-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_report(path) -> dict | None:
    try:
        path = Path(path)
        if not path.is_file() or path.stat().st_size > 100_000:
            return None
        return safe_report(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError):
        return None
