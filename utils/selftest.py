"""Offline installed-runtime diagnostics, suitable for a user's Termux session.

Synthetic checkpoint fixtures stay in temporary files. The only sockets opened
are the temporary dashboard and its client on 127.0.0.1; no browser is launched.
"""

from __future__ import annotations

import http.client
import importlib
import json
import os
import platform
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path


def _check_crypto():
    aes = importlib.import_module("Crypto.Cipher.AES")
    from tester.telegram_session import _ige

    key = bytes(range(32))
    plain = bytes.fromhex("00112233445566778899aabbccddeeff")
    expected = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")  # FIPS-197 AES-256 example.
    encrypted = aes.new(key, aes.MODE_ECB).encrypt(plain)
    if encrypted != expected or aes.new(key, aes.MODE_ECB).decrypt(expected) != plain:
        raise ValueError("AES known-answer check failed")
    payload, iv = bytes(range(64)), bytes(range(32))
    encoded = _ige(payload, key, iv)
    if encoded == payload or _ige(encoded, key, iv, decrypt=True) != payload:
        raise ValueError("MTProto AES-IGE runtime roundtrip failed")


def _check_parser():
    from collector.sources import parse_text_blob

    # Reserved documentation address: a format fixture, never a live candidate.
    parsed = parse_text_blob(
        "https://t.me/proxy?server=192.0.2.1&port=443&secret=3XnnAQIAAQAH8AMDhuJMOt0",
        "DEMO offline selftest",
    )
    if len(parsed) != 1 or parsed[0].server != "192.0.2.1" or parsed[0].port != 443:
        raise ValueError("Telegram HTTPS proxy-link parser check failed")


def _check_web_parser():
    from telegram_webproxy import TelegramWebProxy, parse_line

    # Fixed protocol vectors; no DNS lookup or connection to these reserved names.
    secret = "000102030405060708090a0b0c0d0e0f"
    for server, expected in (
        ("proxy.example.com", "MHLEY5PmW1GWqJkSrlmJpvJUiLhBH_QKy6yKg8a0JPk"),
        ("proxy.example.com/dobry-cola-super-app", "hHz99Xs93EN1j91G9gpNepXwGNNt5YdAFkEVk_LlqdQ"),
    ):
        proxy = TelegramWebProxy(server, secret, source="DEMO offline selftest")
        if proxy.bridge_capability != expected:
            raise ValueError("Telegram WEB capability known-answer check failed")
        for link in (proxy.tg_link(), proxy.web_link()):
            parsed = parse_line(link)
            if parsed is None or parsed.key() != proxy.key():
                raise ValueError("Telegram WEB marked link roundtrip failed")
    if parse_line(f"tg://webproxy?server=proxy.example.com/path&secret={secret}") is not None:
        raise ValueError("Telegram WEB accepted an unmarked path secret")
    if parse_line(f"https://t.me/proxy?server=proxy.example.com&secret={secret}") is not None:
        raise ValueError("Telegram WEB confused an ordinary MTProxy link")


def _check_checkpoint():
    from utils.models import Proxy, ProxyResult, TestSample
    from utils.scan_state import CheckpointStore, deserialize_result, serialize_result

    proxy = Proxy("192.0.2.1", 443, "00112233445566778899aabbccddeeff", "DEMO offline selftest")
    result = ProxyResult(proxy, [TestSample(True, total_latency_ms=12.5)])
    with tempfile.TemporaryDirectory(prefix="revmamad-selftest-checkpoint-") as folder:
        store = CheckpointStore(
            Path(folder), mode="mtproto", network="selftest", fingerprint="a" * 64, max_age_seconds=60
        )
        store.save(
            [proxy], {"scope": "DEMO synthetic offline fixture", "results": [serialize_result(result)]}
        )
        candidates, state = store.load()
        restored = deserialize_result(state["results"][0])
        if (
            len(candidates) != 1
            or candidates[0].key() != proxy.key()
            or restored.proxy.key() != proxy.key()
            or restored.samples[0].total_latency_ms != 12.5
            or restored.strict_eligible
            or list(Path(folder).glob("*.tmp"))
        ):
            raise ValueError("Atomic checkpoint restore check failed")


def _http_get(port: int, path: str) -> tuple[str, bytes]:
    # HTTPConnection deliberately bypasses any HTTP_PROXY environment routing.
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        if response.status != 200 or len(content) > 1024 * 1024:
            raise ValueError("Invalid local dashboard response")
        return response.getheader("Content-Type", ""), content
    finally:
        connection.close()


def _check_dashboard():
    from dashboard.server import create_server

    with tempfile.TemporaryDirectory(prefix="revmamad-selftest-dashboard-") as folder:
        server = create_server("127.0.0.1", 0, folder)
        worker = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05),
            name="revmamad-selftest-dashboard",
            daemon=True,
        )
        started = False
        try:
            worker.start()
            started = True
            for path, content_type, marker in (
                ("/", "text/html", b"REVMAMAD"),
                ("/dashboard.js", "application/javascript", b"/api/snapshot"),
                ("/styles.css", "text/css", b"{"),
            ):
                actual_type, body = _http_get(server.server_port, path)
                if not actual_type.startswith(content_type) or marker not in body:
                    raise ValueError("Local dashboard asset check failed")
            content_type, body = _http_get(server.server_port, "/api/snapshot")
            snapshot = json.loads(body)
            if (
                not content_type.startswith("application/json")
                or not isinstance(snapshot, dict)
                or snapshot.get("rows") != []
                or snapshot.get("mode") != "mtproto"
            ):
                raise ValueError("Empty local dashboard snapshot check failed")
        finally:
            if started:
                server.shutdown()
            server.server_close()
            if started:
                worker.join(timeout=2)
                if worker.is_alive():
                    raise RuntimeError("Local dashboard thread did not stop")


def _http_control(port: int, body: dict, token: str, *, origin: str | None = None) -> tuple[int, dict]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request(
            "POST",
            "/api/control",
            json.dumps(body),
            {
                "Content-Type": "application/json",
                "Origin": origin or f"http://127.0.0.1:{port}",
                "X-Revmamad-Token": token,
            },
        )
        response = connection.getresponse()
        content = response.read(1024 * 1024 + 1)
        if len(content) > 1024 * 1024:
            raise ValueError("Oversized local dashboard control response")
        return response.status, json.loads(content)
    finally:
        connection.close()


def _check_dashboard_controls():
    from config.loader import AppConfig
    from dashboard.control import ScanController
    from dashboard.server import create_server

    # Exercise actual HTTP/CSRF/worker/stop plumbing without running a live scan.
    entered, finished = threading.Event(), threading.Event()
    calls = []

    def execute(config, mode, limit, command, resume, stop_event):
        calls.append((config.scan.network, mode, limit, command, resume))
        entered.set()
        try:
            if not stop_event.wait(3):
                raise TimeoutError("Local diagnostic scan was not stopped")
        finally:
            finished.set()

    config = AppConfig.load()
    config.scan.network = "selftest"
    controller = ScanController(config, execute)
    with tempfile.TemporaryDirectory(prefix="revmamad-selftest-controls-") as folder:
        server = create_server("127.0.0.1", 0, folder, controller=controller)
        worker = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.05),
            name="revmamad-selftest-controls",
            daemon=True,
        )
        started = False
        try:
            worker.start()
            started = True
            _, content = _http_get(server.server_port, "/api/control")
            controls = json.loads(content)
            token = controls.get("token")
            if controls.get("enabled") is not True or not isinstance(token, str) or len(token) != 43:
                raise ValueError("Local control token check failed")
            body = {"action": "start", "mode": "webproxy", "count": 1, "network": "selftest"}
            for bad_token, bad_origin in (
                ("invalid-selftest-token", None),
                (token, "https://example.invalid"),
            ):
                status, _ = _http_control(server.server_port, body, bad_token, origin=bad_origin)
                if status != 403 or entered.is_set():
                    raise ValueError("Local controls accepted an invalid token or origin")
            status, response = _http_control(server.server_port, body, token)
            if status != 202 or response.get("job", {}).get("state") != "running" or not entered.wait(1):
                raise ValueError("Local diagnostic start failed")
            status, response = _http_control(
                server.server_port,
                {"action": "stop", "mode": "webproxy", "network": "selftest"},
                token,
            )
            if status != 202 or response.get("job", {}).get("state") != "stopping":
                raise ValueError("Local diagnostic stop failed")
            controller.close(timeout=2)
            if (
                not finished.is_set()
                or controller.status().get("job", {}).get("state") != "stopped"
                or calls != [("selftest", "webproxy", 1, "best10", False)]
            ):
                raise ValueError("Local diagnostic worker did not stop correctly")
        finally:
            controller.close(timeout=2)
            if started:
                server.shutdown()
            server.server_close()
            if started:
                worker.join(timeout=2)
                if worker.is_alive():
                    raise RuntimeError("Local control thread did not stop")


def run_selftest(config) -> int:
    """Print PASS/FAIL, persist a credential-free JSON report, and return 0 or 1."""
    from main import __version__

    checks = []
    dependency_failed = False
    jobs = [
        (f"dependency_{module}", lambda name=module: importlib.import_module(name))
        for module in ("yaml", "httpx", "Crypto", "idna")
    ]
    jobs.extend(
        [
            ("scan_config", lambda: config.scan.validate()),
            ("crypto_runtime", _check_crypto),
            ("telegram_link_parser", _check_parser),
            ("telegram_web_link_parser", _check_web_parser),
            ("atomic_checkpoint", _check_checkpoint),
            ("dashboard_loopback", _check_dashboard),
            ("dashboard_controls_loopback", _check_dashboard_controls),
        ]
    )
    print("REVMAMAD selftest: offline runtime/loopback only")
    for name, check in jobs:
        try:
            check()
            row = {"name": name, "status": "pass", "error_class": None}
            print(f"PASS {name}")
        except Exception as exc:  # A failed diagnostic must not hide the remaining checks.
            row = {"name": name, "status": "fail", "error_class": type(exc).__name__}
            dependency_failed = dependency_failed or isinstance(exc, ImportError)
            print(f"FAIL {name} ({type(exc).__name__})")
        checks.append(row)
    report = {
        "version": __version__,
        "python": platform.python_version(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "termux": bool(os.environ.get("TERMUX_VERSION"))
        or "/com.termux/" in str(os.environ.get("PREFIX", "")) + sys.executable,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "scope": "offline runtime/loopback only",
        "telegram_live_tested": False,
        "android_browser_intent_tested": False,
        "physical_android_workflows_tested": False,
        "checks": checks,
        "all_passed": all(row["status"] == "pass" for row in checks),
    }
    if dependency_failed:
        print("Install missing runtime packages: python -m pip install -r requirements-lite.txt")
    try:
        from scan import network_folder
        from utils.scan_state import write_json_atomic

        valid_scan = next(row["status"] == "pass" for row in checks if row["name"] == "scan_config")
        folder = network_folder(config) if valid_scan else Path(config.output.folder)
        path = folder / "selftest-report.json"
        write_json_atomic(path, report)
        print(f"Report: {path}")
    except Exception as exc:
        print(f"FAIL report_write ({type(exc).__name__})")
        return 1
    return 0 if report["all_passed"] else 1
