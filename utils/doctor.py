"""Allowlisted local diagnostics: never package logs, configuration or account data."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEPENDENCIES = {"httpx": "httpx", "idna": "idna", "yaml": "PyYAML", "Crypto": "pycryptodome"}
FAILURES = {
    "dns_error",
    "tcp_timeout",
    "tcp_refused",
    "tls_error",
    "handshake_timeout",
    "handshake_invalid",
    "secret_invalid",
    "unsupported_transport",
    "client_incompatible",
    "unknown",
}


def _bounded_json(path, limit=1024 * 1024):
    with Path(path).open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Report exceeds its read limit")
    return json.loads(content)


def collect_diagnostics(config) -> dict:
    from utils.process_lock import OutputRootBusy, OutputRootLock

    checks = []
    for module, distribution in DEPENDENCIES.items():
        available = importlib.util.find_spec(module) is not None
        try:
            version = importlib.metadata.version(distribution) if available else None
        except importlib.metadata.PackageNotFoundError:
            version = "unknown"
        checks.append({"name": module, "status": "pass" if available else "fail", "version": version})
    try:
        config.scan.validate()
        checks.append({"name": "scan_configuration", "status": "pass"})
    except (ValueError, TypeError):
        checks.append({"name": "scan_configuration", "status": "fail"})
    folder = Path(config.output.folder)
    try:
        with OutputRootLock(folder, operation="doctor"):
            with tempfile.TemporaryFile(dir=folder) as probe:
                probe.write(b"revmamad diagnostic")
                probe.flush()
                os.fsync(probe.fileno())
        checks.append({"name": "output_write_and_lock", "status": "pass"})
    except OutputRootBusy:
        checks.append({"name": "output_write_and_lock", "status": "busy"})
    except OSError:
        checks.append({"name": "output_write_and_lock", "status": "fail"})
    observations = []
    # Exact bounded paths, never recursively collect arbitrary user files.
    from scan import mode_folder

    for mode in ("mtproto", "telegram", "web", "webproxy"):
        try:
            raw = _bounded_json(mode_folder(config, mode) / "snapshot.json")
        except (OSError, ValueError, TypeError):
            continue
        summary = raw.get("summary", {}) if isinstance(raw, dict) else {}
        if not isinstance(summary, dict):
            continue
        item = {"mode": mode}
        for key in ("selected", "tested", "eligible", "fully_tested", "partial_tested"):
            number = summary.get(key)
            if type(number) is int and 0 <= number <= 1_000_000:
                item[key] = number
        failures = summary.get("failure_counts", {})
        if isinstance(failures, dict):
            item["failures"] = {
                name: count
                for name, count in failures.items()
                if name in FAILURES and type(count) is int and 0 <= count <= 100_000_000
            }
        observations.append(item)
    try:
        free_mib = shutil.disk_usage(folder).free // (1024 * 1024)
    except OSError:
        free_mib = None
    return {
        "schema_version": 1,
        "kind": "redacted_local_diagnostics",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "python": platform.python_version(),
            "platform": sys.platform,
            "termux": "TERMUX_VERSION" in os.environ or "com.termux" in os.environ.get("PREFIX", ""),
            "free_disk_mib": free_mib,
        },
        "checks": checks,
        "observations": observations,
        "live_network_tested": False,
        "physical_device_verified": False,
        "account_tested": False,
        "all_passed": all(row["status"] == "pass" for row in checks),
    }


def write_diagnostics(path: Path, report: dict):
    """Only reports freshly built from the allowlist belong here."""
    from config.loader import OutputConfig
    from exporter.exporter import Exporter

    Exporter(OutputConfig(folder=str(path.parent)))._write(
        path.name, json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
