"""Validated, mode-isolated snapshot data shared by both dashboard adapters."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

DEFAULT_OUTPUT_FOLDER = Path(__file__).resolve().parents[1] / "output"
DEFAULT_DATA_FILE = DEFAULT_OUTPUT_FOLDER / "proxy.json"  # v2 compatibility
MODES = ("mtproto", "web")
PROTOCOLS = ("http", "https", "socks4", "socks5")
SORT_FIELDS = ("score", "avg_latency_ms", "success_rate", "stability")
COUNT_FIELDS = ("collected", "selected", "tested", "verified", "eligible", "displayed")


def number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return value
    return None


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError("mode must be mtproto or web")
    return mode


def _endpoint_key(row: dict, mode: str) -> str:
    """Match Proxy.key/WebProxy.key, including transport and secret identity."""
    server = row["server"]
    if mode == "web":
        host = f"[{server}]" if ":" in server else server
        return f"{row.get('protocol', '')}://{host}:{row.get('port', '')}"
    return f"{server}:{row.get('port', '')}:{row.get('secret', '')}"


def _safe_action(row: dict, mode: str) -> str | None:
    value = row.get("tg_link" if mode == "mtproto" else "uri")
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) <= 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        server = row["server"].strip("[]").casefold()
        port = row.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            return None
        if parsed.fragment or parsed.username is not None or parsed.password is not None:
            return None
        if mode == "web":
            if (
                parsed.scheme not in PROTOCOLS
                or parsed.scheme != row.get("protocol")
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.hostname is None
                or parsed.hostname.casefold() != server
                or parsed.port != port
            ):
                return None
        else:
            if parsed.scheme != "tg" or parsed.netloc != "proxy" or parsed.path:
                return None
            query = parse_qs(parsed.query)
            if any(len(query.get(key, [])) != 1 for key in ("server", "port", "secret")):
                return None
            if query["server"][0].strip("[]").casefold() != server or query["port"][0] != str(port):
                return None
            secret = query["secret"][0]
            if not re.fullmatch(r"[A-Za-z0-9_+/=-]{16,600}", secret):
                return None
            if row.get("secret") is not None and secret != row["secret"]:
                return None
        return value
    except (ValueError, KeyError, TypeError):
        return None


def _rows(rows: list, mode: str) -> list[dict]:
    result = []
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or not isinstance(row.get("server"), str) or not row["server"]:
            continue
        try:
            json.dumps(row, allow_nan=False)
        except (ValueError, TypeError, RecursionError):
            continue
        item = dict(row)
        item["rank"] = index
        for field in (*SORT_FIELDS, "timeout_rate", "median_latency_ms", "jitter_ms"):
            item[field] = number(row.get(field))
        item["source"] = str(row.get("source") or "unknown")
        if mode == "mtproto":
            item.pop("uri", None)
            item.pop("protocol", None)
            item["tg_link"] = _safe_action(row, mode)
        else:
            item.pop("tg_link", None)
            item.pop("secret", None)
            item["uri"] = _safe_action(row, mode)
        result.append(item)
    return result


def load_data(path: Path) -> list[dict]:
    """Legacy v2 JSON-list reader retained for old callers."""
    try:
        rows = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return []
    return _rows(rows, "mtproto") if isinstance(rows, list) else []


def _empty(mode: str, state: str, message: str) -> dict:
    return {
        "version": 1,
        "mode": mode,
        "state": state,
        "message": message,
        "summary": {},
        "source_reports": [],
        "rows": [],
    }


def _validate_summary(summary: dict) -> None:
    for field in COUNT_FIELDS:
        value = summary.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("Invalid scan counts")
    requested = summary.get("requested_count")
    if requested is not None and (
        isinstance(requested, bool) or not isinstance(requested, int) or requested < 1
    ):
        raise ValueError("Invalid requested count")
    for field in ("started_at", "completed_at"):
        value = summary.get(field)
        if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("Invalid scan timestamp")
    failures = summary.get("failure_counts")
    if not isinstance(failures, dict) or any(
        not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0
        for key, value in failures.items()
    ):
        raise ValueError("Invalid failure counts")


def _validate_health(rows: list) -> None:
    """A completed v3 snapshot must not label explicit failed metrics eligible."""
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Invalid snapshot row")
        for field in (*SORT_FIELDS, "timeout_rate", "median_latency_ms", "jitter_ms"):
            if field not in row:
                continue
            value = number(row[field])
            if value is None or value < 0:
                raise ValueError("Invalid result metric")
            if field in ("score", "success_rate", "stability", "timeout_rate") and value > 1:
                raise ValueError("Invalid result rate")
            if field == "success_rate" and value == 0:
                raise ValueError("Failed result is not eligible")
        for field in ("attempts", "successes"):
            if field in row and (type(row[field]) is not int or row[field] < 1):
                raise ValueError("Failed result is not eligible")
        if "attempts" in row and "successes" in row and row["successes"] > row["attempts"]:
            raise ValueError("Invalid result attempts")


def snapshot(output_folder: str | Path, mode: str = "mtproto") -> dict:
    """Read summary and rows exactly once; never fall back across proxy modes."""
    mode = validate_mode(mode)
    root = Path(output_folder)
    path = root / mode / "snapshot.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        legacy = root / "proxy.json"
        if mode == "mtproto" and legacy.is_file():
            try:
                raw_legacy = json.loads(legacy.read_text(encoding="utf-8"))
                if not isinstance(raw_legacy, list):
                    raise ValueError("Invalid legacy results")
                rows = _rows(raw_legacy, mode)
                if len(rows) != len(raw_legacy):
                    raise ValueError("Invalid legacy row")
            except (OSError, ValueError, UnicodeError, RecursionError):
                return _empty(
                    mode, "corrupt", "The legacy results cannot be read. Run a new scan in the terminal."
                )
            return {
                "version": 1,
                "mode": mode,
                "state": "legacy",
                "message": "Legacy MTProto results; scan time and candidate counts are unknown.",
                "summary": {
                    "started_at": None,
                    "completed_at": None,
                    "requested_count": None,
                    "collected": None,
                    "selected": None,
                    "tested": None,
                    "verified": None,
                    "eligible": len(rows),
                    "displayed": min(10, len(rows)),
                    "failure_counts": {},
                },
                "source_reports": [],
                "rows": rows,
            }
        return _empty(mode, "missing", "No completed scan for this mode. Start a scan in the terminal.")
    except (OSError, ValueError, UnicodeError, RecursionError):
        return _empty(mode, "corrupt", "The saved snapshot cannot be read. Run a new scan in the terminal.")
    try:
        if (
            not isinstance(raw, dict)
            or type(raw.get("version")) is not int
            or raw["version"] != 1
            or raw.get("mode") != mode
            or not isinstance(raw.get("rows"), list)
            or not isinstance(raw.get("summary"), dict)
            or not isinstance(raw.get("source_reports"), list)
        ):
            raise ValueError("Invalid snapshot schema")
        json.dumps(raw, allow_nan=False)
        _validate_summary(raw["summary"])
        _validate_health(raw["rows"])
        reports = []
        for report in raw["source_reports"]:
            if (
                not isinstance(report, dict)
                or not isinstance(report.get("name"), str)
                or report.get("status") not in ("ok", "empty", "error")
                or type(report.get("count")) is not int
                or report["count"] < 0
            ):
                raise ValueError("Invalid source report")
            safe = {key: report[key] for key in ("name", "status", "count")}
            if isinstance(report.get("error"), str):
                safe["error"] = report["error"][:200]
            reports.append(safe)
        rows = _rows(raw["rows"], mode)
        if len(rows) != len(raw["rows"]):
            raise ValueError("Invalid snapshot row")
        if raw["summary"]["eligible"] != len(rows) or raw["summary"]["displayed"] != min(10, len(rows)):
            raise ValueError("Snapshot counts do not match its eligible rows")
        return {
            "version": 1,
            "mode": mode,
            "state": "ready",
            "message": "Completed scan snapshot.",
            "summary": dict(raw["summary"]),
            "source_reports": reports,
            "rows": rows,
        }
    except (ValueError, TypeError, OverflowError, RecursionError):
        return _empty(
            mode, "corrupt", "The saved snapshot has an invalid format. Run a new scan in the terminal."
        )


def filter_rows(
    rows: list[dict],
    search: str = "",
    sort_by: str = "score",
    order: str = "desc",
    limit: int = 200,
    mode: str = "mtproto",
    protocol: str = "all",
    preserve_rank: bool = False,
) -> list[dict]:
    validate_mode(mode)
    if (
        sort_by not in SORT_FIELDS
        or order not in ("asc", "desc")
        or type(limit) is not int
        or not 1 <= limit <= 5000
        or protocol not in ("all", *PROTOCOLS)
        or mode == "mtproto"
        and protocol != "all"
    ):
        raise ValueError("Invalid sort_by, order, limit, or protocol")
    if search:
        term = search.casefold()
        rows = [row for row in rows if term in row["server"].casefold() or term in row["source"].casefold()]
    if protocol != "all":
        rows = [row for row in rows if row.get("protocol") == protocol]
    if preserve_rank and sort_by == "score" and order == "desc":
        return rows[:limit]
    known = [row for row in rows if row[sort_by] is not None]
    unknown = [row for row in rows if row[sort_by] is None]
    known.sort(
        key=lambda row: (
            -(row[sort_by]) if order == "desc" else row[sort_by],
            row["avg_latency_ms"] if row["avg_latency_ms"] is not None else math.inf,
            _endpoint_key(row, mode),
        )
    )
    return (known + unknown)[:limit]


def proxies(
    path: Path,
    search: str = "",
    sort_by: str = "score",
    order: str = "desc",
    limit: int = 200,
    mode: str = "mtproto",
    protocol: str = "all",
):
    validate_mode(mode)
    path = Path(path)
    if path.suffix == ".json" and mode == "mtproto":
        return filter_rows(load_data(path), search, sort_by, order, limit, mode, protocol)
    current = snapshot(path, mode)
    return filter_rows(
        current["rows"], search, sort_by, order, limit, mode, protocol, current["state"] == "ready"
    )


def row_stats(rows: list[dict], mode: str = "mtproto", preserve_rank: bool = False) -> dict:
    healthy = [row for row in rows if (row["success_rate"] or 0) > 0]
    latencies = [row["avg_latency_ms"] for row in healthy if row["avg_latency_ms"] is not None]
    scored = [row for row in rows if row["score"] is not None]
    ordered = (
        scored
        if preserve_rank
        else sorted(
            scored,
            key=lambda row: (
                -row["score"],
                row["avg_latency_ms"] if row["avg_latency_ms"] is not None else math.inf,
                _endpoint_key(row, mode),
            ),
        )
    )
    return {
        "total_proxies": len(rows),
        "healthy_proxies": len(healthy),
        "average_latency_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "best_proxy": ordered[0] if ordered else None,
        "worst_proxy": ordered[-1] if ordered else None,
    }


def stats(path: Path, mode: str = "mtproto"):
    validate_mode(mode)
    path = Path(path)
    if path.suffix == ".json" and mode == "mtproto":
        return row_stats(load_data(path), mode)
    current = snapshot(path, mode)
    return row_stats(current["rows"], mode, current["state"] == "ready")


def api_response(route: str, output_folder: Path, parameters: dict[str, list[str]]):
    """Shared query validation and data semantics for stdlib and FastAPI."""
    allowed = {"mode"}
    if route == "/api/proxies":
        allowed.update({"search", "sort_by", "order", "limit", "protocol"})
    if set(parameters) - allowed or any(len(values) != 1 for values in parameters.values()):
        raise ValueError("Invalid or repeated query parameters")

    def value(key, default):
        return parameters.get(key, [default])[0]

    mode = validate_mode(value("mode", "mtproto"))
    current = snapshot(output_folder, mode)
    if route == "/api/snapshot":
        return current
    if route == "/api/stats":
        return row_stats(current["rows"], mode, current["state"] == "ready")
    if route == "/api/proxies":
        return filter_rows(
            current["rows"],
            value("search", ""),
            value("sort_by", "score"),
            value("order", "desc"),
            int(value("limit", "200")),
            mode,
            value("protocol", "all"),
            current["state"] == "ready",
        )
    raise ValueError("Unknown API route")
