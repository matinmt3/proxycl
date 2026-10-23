"""Validated, mode-isolated snapshot data shared by both dashboard adapters."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from config.loader import ScanConfig
from tester.secrets import decode_secret, normalize_port, normalize_server
from utils.evidence import saved_evidence_status

DEFAULT_OUTPUT_FOLDER = Path(__file__).resolve().parents[1] / "output"
DEFAULT_DATA_FILE = DEFAULT_OUTPUT_FOLDER / "proxy.json"  # v2 compatibility
MODES = ("mtproto", "telegram", "webproxy", "web")
PROTOCOLS = ("http", "https", "socks4", "socks5")
SORT_FIELDS = ("score", "avg_latency_ms", "success_rate", "stability", "telegram_p95_ms")
COUNT_FIELDS = ("collected", "selected", "tested", "verified", "eligible", "displayed")
SESSION_METRICS = ("telegram_median_ms", "telegram_p95_ms", "telegram_jitter_ms", "reliability_lower_bound")
AUXILIARY_MAX_BYTES = 1024 * 1024
PROGRESS_MAX_AGE_SECONDS = 180
STAGES = ("prefilter", "baseline", "finalists", "testing", "complete", "stopped")


def number(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
            return value
    except OverflowError:
        pass
    return None


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError("mode must be mtproto, telegram, webproxy, or web")
    return mode


def _strict_qualified(row: dict) -> bool:
    """Check saved proof metadata against its policy; a profile label alone is insufficient."""
    policy = row.get("strict_policy")
    if (
        row.get("verification_profile") != "telegram_session"
        or row.get("strict_eligible") is not True
        or not isinstance(policy, dict)
    ):
        return False
    required = policy.get("required_dc_ids")
    verified = row.get("dc_ids_verified")
    for values in (required, verified):
        if (
            not isinstance(values, list)
            or any(type(value) is not int or not 1 <= value <= 5 for value in values)
            or len(values) != len(set(values))
        ):
            return False
    if len(set(required)) < 2 or not set(required).issubset(verified):
        return False
    minimum = policy.get("min_successes")
    pings = policy.get("min_ping_count")
    rate = number(policy.get("min_success_rate"))
    if (
        type(minimum) is not int
        or minimum < 3
        or type(pings) is not int
        or pings < 3
        or rate is None
        or not 0.8 <= rate <= 1
    ):
        return False
    attempts, successes, strict_successes = (
        row.get(key) for key in ("attempts", "successes", "strict_successes")
    )
    if (
        any(type(value) is not int for value in (attempts, successes, strict_successes))
        or not minimum <= strict_successes <= successes <= attempts
    ):
        return False
    observed = strict_successes / attempts
    reported_rate = number(row.get("success_rate"))
    if reported_rate is None or abs(reported_rate - successes / attempts) > 0.0001:
        return False
    count = row.get("ping_count")
    if observed < rate or type(count) is not int or count < strict_successes * pings:
        return False
    metrics = [number(row.get(key)) for key in SESSION_METRICS]
    if any(value is None or value < 0 for value in metrics):
        return False
    median, p95, jitter, lower_bound = metrics
    max_p95, max_jitter = (number(policy.get(key)) for key in ("max_p95_ms", "max_jitter_ms"))
    if max_p95 is None or max_p95 <= 0 or max_jitter is None or max_jitter <= 0:
        return False
    z2 = 1.96**2
    wilson = (
        observed
        + z2 / (2 * attempts)
        - 1.96 * math.sqrt((observed * (1 - observed) + z2 / (4 * attempts)) / attempts)
    ) / (1 + z2 / attempts)
    return median <= p95 <= max_p95 and jitter <= max_jitter and abs(lower_bound - wilson) <= 0.0001


def _endpoint_key(row: dict, mode: str) -> str:
    """Match Proxy.key/WebProxy.key, including transport and secret identity."""
    server = row["server"]
    if mode == "web":
        host = f"[{server}]" if ":" in server else server
        return f"{row.get('protocol', '')}://{host}:{row.get('port', '')}"
    if mode == "webproxy":
        return f"webproxy:{server}:443:{row.get('secret', '')}"
    return f"{server}:{row.get('port', '')}:{row.get('secret', '')}"


def _safe_action(row: dict, mode: str, *, require_secret: bool = False) -> str | None:
    value = row.get(
        {"mtproto": "tg_link", "telegram": "web_link", "webproxy": "web_link", "web": "uri"}[mode]
    )
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
        if mode == "webproxy":
            from telegram_webproxy.models import TelegramWebProxy
            from telegram_webproxy.parser import parse_line

            if parsed.scheme != "https" or parsed.hostname not in ("t.me", "telegram.me"):
                return None
            proxy = parse_line(value)
            expected = TelegramWebProxy(row["server"], row["secret"], port=port)
            if proxy is None or proxy.key() != expected.key() or row.get("protocol") != "webproxy":
                return None
        elif mode == "web":
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
            if mode == "telegram":
                if (
                    parsed.scheme != "https"
                    or parsed.hostname != "t.me"
                    or parsed.port not in (None, 443)
                    or parsed.path != "/proxy"
                ):
                    return None
            elif parsed.scheme != "tg" or parsed.netloc != "proxy" or parsed.path:
                return None
            query = parse_qs(parsed.query, keep_blank_values=True)
            if any(len(query.get(key, [])) != 1 for key in ("server", "port", "secret")):
                return None
            if mode == "telegram" and set(query) != {"server", "port", "secret"}:
                return None
            if query["server"][0].strip("[]").casefold() != server or query["port"][0] != str(port):
                return None
            secret = query["secret"][0]
            if not re.fullmatch(r"[A-Za-z0-9_+/=-]{16,600}", secret):
                return None
            if mode == "telegram" or require_secret:
                if not isinstance(row.get("secret"), str):
                    return None
                if decode_secret(secret).encoded != decode_secret(row["secret"]).encoded:
                    return None
            elif row.get("secret") is not None and secret != row["secret"]:
                return None
        return value
    except (ValueError, KeyError, TypeError):
        return None


def _rows(rows: list, mode: str, profile: str = "transport") -> list[dict]:
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
        try:
            from utils.scan_state import deserialize_proxy

            family = "mtproto" if mode in {"mtproto", "telegram"} else mode
            candidate = deserialize_proxy(
                {**row, "kind": family, "protocol": "mtproto" if family == "mtproto" else row.get("protocol")}
            )
            item["candidate_id"] = hashlib.sha256(candidate.key().encode()).hexdigest()
        except (ValueError, TypeError):
            item["candidate_id"] = None
        for field in (*SORT_FIELDS, "timeout_rate", "median_latency_ms", "jitter_ms", *SESSION_METRICS):
            item[field] = number(row.get(field))
        item["source"] = str(row.get("source") or "unknown")
        item["evidence_status"] = saved_evidence_status(row)
        if mode != "web":
            qualified = profile == "telegram_session" and _strict_qualified(row)
            item["verification_profile"] = "telegram_session" if qualified else "transport"
            item["strict_eligible"] = qualified
            item.pop("uri", None)
            if mode == "webproxy":
                item["protocol"] = "webproxy"
                item["web_link"] = _safe_action(row, mode) if qualified else None
                if qualified and item["web_link"]:
                    from telegram_webproxy.parser import parse_line

                    item["tg_link"] = parse_line(item["web_link"]).tg_link()
                else:
                    item["tg_link"] = None
            else:
                item.pop("protocol", None)
                item["tg_link"] = (
                    _safe_action(row, "mtproto", require_secret=mode == "telegram") if qualified else None
                )
            if mode != "webproxy" and (mode == "telegram" or "web_link" in item):
                item["web_link"] = _safe_action(row, "telegram") if qualified else None
        else:
            item.pop("tg_link", None)
            item.pop("web_link", None)
            item.pop("secret", None)
            for key in list(item):
                if key.startswith(("strict_", "telegram_")) or key in {
                    "dc_ids_verified",
                    "ping_count",
                    "reliability_lower_bound",
                }:
                    item.pop(key)
            item["verification_profile"] = "web"
            item["uri"] = _safe_action(row, mode)
        if item["evidence_status"] != "fresh":
            for action in ("uri", "tg_link", "web_link"):
                if action in item:
                    item[action] = None
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
    interrupted = summary.get("interrupted", False)
    if type(interrupted) is not bool or summary["tested"] > summary["selected"]:
        raise ValueError("Invalid interrupted scan metadata")
    skipped = summary.get("skipped", summary["selected"] - summary["tested"])
    if type(skipped) is not int or skipped != summary["selected"] - summary["tested"]:
        raise ValueError("Invalid skipped count")
    if not interrupted and skipped:
        raise ValueError("Unfinished scan cannot be marked complete")
    retries = summary.get("retry_count")
    if "retry_count" in summary and (type(retries) is not int or retries < 1):
        raise ValueError("Invalid retry count")
    if "fully_tested" in summary or "partial_tested" in summary:
        fully, partial = summary.get("fully_tested"), summary.get("partial_tested")
        if (
            type(fully) is not int
            or type(partial) is not int
            or not 0 <= fully <= summary["tested"]
            or not 0 <= partial <= summary["tested"]
            or fully + partial != summary["tested"]
            or retries == 1
            and partial != 0
        ):
            raise ValueError("Invalid complete/partial retry counts")
    if "strategy" in summary and summary["strategy"] not in ("staged", "single"):
        raise ValueError("Invalid scan strategy")
    if "stage" in summary and summary["stage"] not in STAGES:
        raise ValueError("Invalid scan stage")
    if "network" in summary and not _network_label(summary["network"]):
        raise ValueError("Invalid scan network")
    for field in ("resumed", "provisional"):
        if field in summary and type(summary[field]) is not bool:
            raise ValueError("Invalid staged scan flag")
    for field in (
        "prefilter_tested",
        "prefilter_passed",
        "baseline_tested",
        "finalist_selected",
        "finalist_completed",
        "finalist_attempts",
    ):
        if field in summary and (type(summary[field]) is not int or summary[field] < 0):
            raise ValueError("Invalid staged scan count")


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


def _saved_snapshot(output_folder: str | Path, mode: str = "mtproto") -> dict:
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
                "message": "Legacy transport probe evidence only. Re-scan for strict verification; scan time and candidate counts are unknown.",
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
                    "verification_profile": "transport",
                    "strict_verified": 0,
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
        if "network" in raw["summary"] and raw["summary"]["network"] != _folder_network(root):
            raise ValueError("Snapshot belongs to another network")
        _validate_health(raw["rows"])
        profile = raw["summary"].get("verification_profile", "web" if mode == "web" else "transport")
        if profile not in (("web",) if mode == "web" else ("telegram_session", "transport")):
            raise ValueError("Invalid verification profile")
        if profile == "telegram_session":
            strict_count, reachable = (
                raw["summary"].get(key) for key in ("strict_verified", "transport_reachable")
            )
            if (
                any(type(value) is not int for value in (strict_count, reachable))
                or not len(raw["rows"]) <= strict_count <= reachable <= raw["summary"]["tested"]
                or raw["summary"]["verified"] != strict_count
                or not all(_strict_qualified(row) for row in raw["rows"])
                or mode == "webproxy"
                and any(
                    row.get("kind") != "webproxy" or row.get("protocol") != "webproxy" for row in raw["rows"]
                )
            ):
                raise ValueError("Invalid strict verification evidence")
        reports = []
        for report in raw["source_reports"]:
            if (
                not isinstance(report, dict)
                or not isinstance(report.get("name"), str)
                or report.get("status") not in ("ok", "empty", "error", "cooldown")
                or type(report.get("count")) is not int
                or report["count"] < 0
            ):
                raise ValueError("Invalid source report")
            safe = {key: report[key] for key in ("name", "status", "count")}
            if isinstance(report.get("error"), str):
                safe["error"] = report["error"][:200]
            for key in ("checked", "qualified", "strict_qualified"):
                if key in report:
                    if type(report[key]) is not int or not 0 <= report[key] <= 100_000:
                        raise ValueError("Invalid source quality count")
                    safe[key] = report[key]
            for key in ("cooldown_until", "cooldown_remaining_seconds"):
                if key in report:
                    value = number(report[key])
                    if value is None or value < 0:
                        raise ValueError("Invalid source cooldown")
                    safe[key] = value
            if report.get("quality_attribution") in {
                "first_source_after_deduplication",
                "all_memberships_non_additive",
            }:
                safe["quality_attribution"] = report["quality_attribution"]
            for key in ("source_id", "publisher_id", "content_id"):
                value = report.get(key)
                if value is not None and (
                    not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)
                ):
                    raise ValueError("Invalid source provenance")
                safe[key] = value
            safe["fetched"] = report.get("fetched") is True
            for key in ("parsed_unique", "mirror_count"):
                value = report.get(key, 0)
                if type(value) is not int or not 0 <= value <= 1_000_000:
                    raise ValueError("Invalid source provenance count")
                safe[key] = value
            reports.append(safe)
        rows = _rows(raw["rows"], mode, profile)
        if raw["summary"].get("provisional"):
            for row in rows:
                row["provisional"] = True
        if len(rows) != len(raw["rows"]):
            raise ValueError("Invalid snapshot row")
        if raw["summary"]["eligible"] != len(rows) or raw["summary"]["displayed"] != min(10, len(rows)):
            raise ValueError("Snapshot counts do not match its eligible rows")
        summary = dict(raw["summary"])
        summary["verification_profile"] = profile
        if profile == "transport":
            summary["strict_verified"] = 0
            summary.setdefault("transport_reachable", summary["verified"])
        summary.setdefault("interrupted", False)
        summary.setdefault("skipped", summary["selected"] - summary["tested"])
        return {
            "version": 1,
            "mode": mode,
            "state": "ready",
            "message": (
                ("Stopped scan / partial results. " if summary["interrupted"] else "")
                + "Transport probe evidence only. Re-scan for strict Telegram verification."
                if profile == "transport"
                else (
                    "Stopped scan / partial results."
                    if summary["interrupted"]
                    else "Completed scan snapshot."
                )
            ),
            "summary": summary,
            "source_reports": reports,
            "rows": rows,
        }
    except (ValueError, TypeError, OverflowError, RecursionError):
        return _empty(
            mode, "corrupt", "The saved snapshot has an invalid format. Run a new scan in the terminal."
        )


def snapshot(output_folder: str | Path, mode: str = "mtproto") -> dict:
    """Return saved proof alongside separately validated progress and history."""
    current = _saved_snapshot(output_folder, mode)
    folder = Path(output_folder)
    network = _folder_network(folder)
    current["network"] = network
    current["progress"], current["progress_state"] = _progress(folder, mode, network)
    current["history"], current["history_state"] = _history(folder, mode, network)
    return current


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
        or mode != "web"
        and protocol != "all"
        or mode == "web"
        and sort_by == "telegram_p95_ms"
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
    allowed = {"mode", "network"}
    if route == "/api/proxies":
        allowed.update({"search", "sort_by", "order", "limit", "protocol"})
    if set(parameters) - allowed or any(len(values) != 1 for values in parameters.values()):
        raise ValueError("Invalid or repeated query parameters")

    def value(key, default):
        return parameters.get(key, [default])[0]

    mode = validate_mode(value("mode", "mtproto"))
    if "network" in parameters:
        network = ScanConfig(network=value("network", "default")).validate().network
        base = Path(output_folder)
        if base.parent.name == "networks" and _network_label(base.name):
            base = base.parent.parent
        output_folder = base if network == "default" else base / "networks" / network
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


def _network_label(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value) is not None


def _folder_network(folder):
    return folder.name if folder.parent.name == "networks" and _network_label(folder.name) else "default"


def _auxiliary(folder, mode, filename, network):
    try:
        with (folder / mode / filename).open("rb") as handle:
            content = handle.read(AUXILIARY_MAX_BYTES + 1)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "corrupt"
    try:
        if len(content) > AUXILIARY_MAX_BYTES:
            raise ValueError("Helper file is too large")
        row = json.loads(content)
        if (
            not isinstance(row, dict)
            or type(row.get("version")) is not int
            or row["version"] != 1
            or row.get("mode") != mode
            or row.get("network") != network
        ):
            raise ValueError("Invalid helper schema or isolation")
        return row, "ready"
    except (ValueError, UnicodeError, RecursionError):
        return None, "corrupt"


def _time_value(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid progress time")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None:
        raise ValueError("Progress time needs timezone")
    return stamp.timestamp()


def _progress(folder, mode, network):
    raw, state = _auxiliary(folder, mode, "progress.json", network)
    if raw is None:
        return None, state
    try:
        timestamp = _time_value(raw.get("updated_at"))
        age = datetime.now(timezone.utc).timestamp() - timestamp
        if age < -60:
            raise ValueError("Future progress")
        run_id = raw.get("run_id")
        if (
            not isinstance(run_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id)
            or raw.get("stage") not in STAGES
            or type(raw.get("active")) is not bool
            or type(raw.get("resumable")) is not bool
        ):
            raise ValueError("Invalid progress metadata")
        from utils.process_lock import output_root_busy

        root = folder.parent.parent if folder.parent.name == "networks" else folder
        active = raw["active"] and raw["stage"] != "complete"
        orphaned = active and not output_root_busy(root)
        if age > (7 * 86400 if orphaned else PROGRESS_MAX_AGE_SECONDS if active else 86400):
            return None, "stale"
        for key in ("completed", "total", "qualified"):
            if type(raw.get(key)) is not int or not 0 <= raw[key] <= 1_000_000:
                raise ValueError("Invalid progress count")
        if raw["completed"] > raw["total"] or raw["qualified"] > raw["total"]:
            raise ValueError("Invalid progress totals")
        remaining = raw.get("remaining_seconds")
        if remaining is not None and (number(remaining) is None or remaining < 0):
            raise ValueError("Invalid remaining time")
        command = raw.get("command", "best10")
        if command not in ("best10", "recheck"):
            raise ValueError("Invalid progress command")
        result = {
            key: raw[key]
            for key in (
                "version",
                "mode",
                "network",
                "run_id",
                "updated_at",
                "stage",
                "completed",
                "total",
                "qualified",
                "active",
                "resumable",
            )
        } | {"remaining_seconds": remaining, "command": command, "recovery_needed": orphaned}
        if orphaned:
            result.update(active=False, stage="stopped", remaining_seconds=None)
        elif result["stage"] == "complete":
            result.update(active=False, resumable=False, remaining_seconds=None)
        return result, "ready"
    except (ValueError, TypeError, OverflowError):
        return None, "corrupt"


def _history(folder, mode, network):
    raw, state = _auxiliary(folder, mode, "history.json", network)
    if raw is None:
        return [], state
    try:
        if not isinstance(raw.get("rows"), list) or len(raw["rows"]) > 20:
            raise ValueError("Invalid history list")
        rows = []
        identities = set()
        now = datetime.now(timezone.utc).timestamp()
        for row in raw["rows"]:
            if not isinstance(row, dict) or row.get("mode") != mode or row.get("network") != network:
                raise ValueError("Invalid history isolation")
            identity = row.get("identity")
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"[a-f0-9]{64}", identity)
                or identity in identities
            ):
                raise ValueError("Invalid history identity")
            identities.add(identity)
            if mode == "webproxy":
                from telegram_webproxy.models import normalize_address

                host, path = normalize_address(row.get("server"))
                server = host + ("/" + path if path else "")
            else:
                server = normalize_server(row.get("server"))
            port = normalize_port(row.get("port"))
            if mode == "webproxy" and port != 443:
                raise ValueError("Invalid WEB history port")
            kind = "web" if mode == "web" else "webproxy" if mode == "webproxy" else "mtproto"
            if row.get("kind") != kind or type(row.get("qualified")) is not bool:
                raise ValueError("Invalid history kind")
            if row.get("protocol") not in (
                PROTOCOLS if mode == "web" else ("webproxy",) if mode == "webproxy" else ("mtproto",)
            ):
                raise ValueError("Invalid history protocol")
            for key in ("successes", "attempts", "run_count", "historical_successes", "historical_attempts"):
                if type(row.get(key)) is not int or not 0 <= row[key] <= 10_000_000:
                    raise ValueError("Invalid history counts")
            if row["successes"] > row["attempts"] or row["historical_successes"] > row["historical_attempts"]:
                raise ValueError("Invalid history rates")
            last = number(row.get("last_tested"))
            p95 = row.get("p95_ms")
            if (
                last is None
                or not 0 < last <= now + 60
                or p95 is not None
                and (number(p95) is None or p95 < 0)
            ):
                raise ValueError("Invalid history time/latency")
            run_id = row.get("current_run_id")
            if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", run_id):
                raise ValueError("Invalid history run")
            safe = {
                key: row[key]
                for key in (
                    "identity",
                    "kind",
                    "protocol",
                    "network",
                    "mode",
                    "last_tested",
                    "successes",
                    "attempts",
                    "qualified",
                    "p95_ms",
                    "current_run_id",
                    "run_count",
                    "historical_successes",
                    "historical_attempts",
                )
            }
            rows.append(safe | {"server": server, "port": port})
        return rows, "ready"
    except (ValueError, TypeError, OverflowError):
        return [], "corrupt"
