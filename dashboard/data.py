"""Data operations shared by desktop and dependency-free mobile dashboards."""

from __future__ import annotations

import json
import math
from pathlib import Path

DEFAULT_DATA_FILE = Path(__file__).resolve().parents[1] / "output" / "proxy.json"
SORT_FIELDS = ("score", "avg_latency_ms", "success_rate", "stability")


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def load_data(path: Path) -> list[dict]:
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return []
    if not isinstance(rows, list):
        return []
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("server"), str):
            continue
        try:
            json.dumps(row, allow_nan=False)
        except (ValueError, TypeError):
            continue
        item = dict(row)
        for field in (*SORT_FIELDS, "timeout_rate", "median_latency_ms", "jitter_ms"):
            item[field] = number(row.get(field))
        item["source"] = str(row.get("source") or "unknown")
        result.append(item)
    return result


def proxies(path: Path, search: str = "", sort_by: str = "score", order: str = "desc", limit: int = 200):
    if sort_by not in SORT_FIELDS or order not in ("asc", "desc") or not 1 <= limit <= 5000:
        raise ValueError("Invalid sort_by, order, or limit")
    rows = load_data(path)
    if search:
        term = search.casefold()
        rows = [row for row in rows if term in row["server"].casefold() or term in row["source"].casefold()]
    known = [row for row in rows if row[sort_by] is not None]
    unknown = [row for row in rows if row[sort_by] is None]
    known.sort(key=lambda row: row[sort_by], reverse=order == "desc")
    return (known + unknown)[:limit]


def stats(path: Path):
    rows = load_data(path)
    healthy = [row for row in rows if (row["success_rate"] or 0) > 0]
    latencies = [row["avg_latency_ms"] for row in healthy if row["avg_latency_ms"] is not None]
    scored = [row for row in rows if row["score"] is not None]
    return {
        "total_proxies": len(rows),
        "healthy_proxies": len(healthy),
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "best_proxy": max(scored, key=lambda row: row["score"], default=None),
        "worst_proxy": min(scored, key=lambda row: row["score"], default=None),
    }
