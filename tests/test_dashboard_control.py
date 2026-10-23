"""Local controls are opt-in, origin-bound and own a single cooperative worker."""

import asyncio
import json
import threading
import time
from http.client import HTTPConnection
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from config.loader import AppConfig
from dashboard.control import ControlError, ScanController
from dashboard.server import create_server


def wait_for(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        threading.Event().wait(0.005)


def test_controller_copies_config_passes_bounded_options_and_saves_before_stopped():
    entered = threading.Event()
    release = threading.Event()
    captured = []
    config = AppConfig.load()

    def execute(current, mode, limit, command, resume, stop):
        captured.append((current, mode, limit, command, resume))
        entered.set()
        assert stop.wait(2)
        assert release.wait(2)

    controller = ScanController(config, execute)
    try:
        job = controller.request(
            {"action": "start", "mode": "webproxy", "network": "MOBILE", "count": 25, "minutes": 3}
        )
        assert entered.wait(1)
        current, mode, limit, command, resume = captured[0]
        assert current is not config and current.scan is not config.scan
        assert current.scan.network == "mobile" and config.scan.network == "default"
        assert current.scan.time_budget_seconds == 180
        assert (mode, limit, command, resume) == ("webproxy", 25, "best10", False)
        with pytest.raises(ControlError) as error:
            controller.request({"action": "recheck"})
        assert error.value.status == 409
        with pytest.raises(ControlError, match="Select the active"):
            controller.request({"action": "stop", "mode": "webproxy", "network": "wifi"})
        stopped = controller.request({"action": "stop", "mode": "webproxy", "network": "mobile"})
        assert stopped["id"] == job["id"] and stopped["state"] == "stopping"
        assert controller.status()["job"]["state"] == "stopping"
        release.set()
        wait_for(lambda: controller.status()["job"]["state"] == "stopped")
    finally:
        release.set()
        controller.close()


def test_resume_has_explicit_scan_or_recheck_target_and_errors_are_private():
    received = []

    def execute(config, mode, limit, command, resume, stop):
        received.append((command, resume, config.scan.resume))
        raise RuntimeError("SECRET_TOKEN=/private/path")

    controller = ScanController(AppConfig.load(), execute)
    try:
        controller.request({"action": "resume", "command": "recheck"})
        wait_for(lambda: controller.status()["job"]["state"] == "failed")
        assert received == [("recheck", True, True)]
        assert "SECRET_TOKEN" not in json.dumps(controller.status())
        controller.close()
        with pytest.raises(ControlError, match="stopped"):
            controller.request({"action": "start"})
    finally:
        controller.close()


@pytest.mark.parametrize("outcome,expected", [(False, "stopped"), (None, "complete"), (True, "complete")])
def test_callback_return_value_has_an_honest_final_state(outcome, expected):
    controller = ScanController(AppConfig.load(), lambda *args: outcome)
    try:
        controller.request({"action": "start"})
        wait_for(lambda: controller.status()["job"]["state"] != "running")
        job = controller.status()["job"]
        assert job["state"] == expected
        assert job["finished_at"] >= job["started_at"]
        assert controller._stop.is_set() is (outcome is False)
    finally:
        controller.close()


@pytest.mark.parametrize(
    "error,expected",
    [
        (KeyboardInterrupt("PRIVATE_SESSION"), "stopped"),
        (asyncio.CancelledError("PRIVATE_SESSION"), "stopped"),
        (SystemExit("PRIVATE_SESSION"), "failed"),
        (RuntimeError("PRIVATE_SESSION"), "failed"),
    ],
)
def test_cancellation_and_exit_always_finalize_without_private_details(error, expected, caplog):
    def execute(*args):
        raise error

    controller = ScanController(AppConfig.load(), execute)
    try:
        controller.request({"action": "start"})
        wait_for(lambda: controller.status()["job"]["state"] != "running")
        job = controller.status()["job"]
        assert job["state"] == expected
        assert job["finished_at"] >= job["started_at"]
        assert "PRIVATE_SESSION" not in json.dumps(job) + caplog.text
    finally:
        controller.close()


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"action": "start"}, None),
        ({"action": "start", "count": "ALL"}, None),
        ({"action": "start", "count": 25}, None),
        ({"action": "start", "minutes": 3}, 180),
        ({"action": "resume"}, 600),
        ({"action": "recheck"}, 600),
    ],
)
def test_new_scan_size_overrides_inherited_time_budget_but_resume_retains_config(body, expected):
    config = AppConfig.load()
    config.scan.time_budget_seconds = 600
    budgets = []
    controller = ScanController(
        config, lambda current, *args: budgets.append(current.scan.time_budget_seconds)
    )
    try:
        controller.request(body)
        wait_for(lambda: controller.status()["job"]["state"] != "running")
        assert budgets == [expected]
        assert config.scan.time_budget_seconds == 600
    finally:
        controller.close()


def test_worker_launch_failure_returns_safe_error_and_close_remains_available(monkeypatch):
    def cannot_start(self):
        raise RuntimeError("PRIVATE_WORKER_SECRET")

    controller = ScanController(AppConfig.load(), lambda *args: None)
    monkeypatch.setattr(threading.Thread, "start", cannot_start)
    try:
        with pytest.raises(ControlError) as error:
            controller.request({"action": "start"})
        assert error.value.status == 503
        assert "PRIVATE_WORKER_SECRET" not in str(error.value) + json.dumps(controller.status())
        assert controller.status()["job"]["state"] == "failed"
    finally:
        controller.close()


@pytest.mark.parametrize(
    "body",
    [
        [],
        {"action": "shell"},
        {"action": "start", "path": "main.py"},
        {"action": "start", "mode": "../web"},
        {"action": "start", "network": "../x"},
        {"action": "start", "network": "CON"},
        {"action": "start", "network": None},
        {"action": "start", "count": True},
        {"action": "start", "count": 0},
        {"action": "start", "count": 1000001},
        {"action": "start", "count": "50"},
        {"action": "start", "minutes": True},
        {"action": "start", "minutes": 2},
        {"action": "resume", "minutes": 3},
        {"action": "recheck", "count": 5},
        {"action": "start", "command": "best10"},
        {"action": "resume", "command": "exec"},
    ],
)
def test_rejected_controls_never_start_a_worker(body):
    calls = []
    controller = ScanController(AppConfig.load(), lambda *args: calls.append(args))
    try:
        with pytest.raises(ControlError):
            controller.request(body)
        assert calls == [] and controller.status()["job"] is None
    finally:
        controller.close()


@pytest.fixture
def local_control(tmp_path):
    started = threading.Event()

    def execute(config, mode, limit, command, resume, stop):
        started.set()
        stop.wait(3)

    controller = ScanController(AppConfig.load(), execute)
    server = create_server("127.0.0.1", 0, tmp_path, controller=controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    yield url, server, controller, started
    controller.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def request(url, controller, body, **headers):
    return Request(
        url + "/api/control",
        data=body if isinstance(body, bytes) else json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Origin": url,
            "X-Revmamad-Token": controller.token,
            **headers,
        },
        method="POST",
    )


def test_live_local_request_stop_and_token_not_in_snapshot_or_assets(local_control):
    url, server, controller, started = local_control
    with urlopen(url + "/api/control", timeout=2) as response:
        info = json.load(response)
        assert info["token"] == controller.token and info["enabled"] is True
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers.get("Access-Control-Allow-Origin") is None
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    with urlopen(request(url, controller, {"action": "start", "mode": "web"}), timeout=2) as response:
        assert response.status == 202
        assert json.load(response)["job"]["state"] == "running"
    assert started.wait(1)
    with urlopen(request(url, controller, {"action": "stop", "mode": "web"}), timeout=2) as response:
        assert json.load(response)["job"]["state"] == "stopping"
    for path in ("/", "/dashboard.js", "/api/snapshot"):
        with urlopen(url + path, timeout=2) as response:
            assert controller.token.encode() not in response.read()


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Origin": "https://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({"Host": "evil.example:8000"}, 403),
        ({"Sec-Fetch-Site": "cross-site"}, 403),
        ({"X-Revmamad-Token": "wrong"}, 403),
        ({"X-Revmamad-Token": "é"}, 403),
        ({"Content-Type": "text/plain"}, 415),
    ],
)
def test_live_cross_site_and_invalid_request_headers_cannot_launch(local_control, headers, status):
    url, server, controller, started = local_control
    with pytest.raises(HTTPError) as error:
        urlopen(request(url, controller, {"action": "start"}, **headers), timeout=2)
    assert error.value.code == status
    assert not started.is_set()


@pytest.mark.parametrize(
    "body,status",
    [
        (b'{"action":"start","action":"stop"}', 400),
        (b"[" * 2000, 400),
        (b"{" + b" " * 4096, 413),
        (b'{"action": "start", "env": "SECRET"}', 400),
    ],
)
def test_live_payload_guards(local_control, body, status):
    url, server, controller, started = local_control
    with pytest.raises(HTTPError) as error:
        urlopen(request(url, controller, body), timeout=2)
    assert error.value.code == status and not started.is_set()


def test_get_token_rejects_cross_site_and_rebinding(local_control):
    url, server, controller, started = local_control
    for headers in (
        {"Sec-Fetch-Site": "cross-site"},
        {"Host": "rebind.example"},
        {"Origin": "http://evil.example"},
    ):
        with pytest.raises(HTTPError) as error:
            urlopen(Request(url + "/api/control", headers=headers), timeout=2)
        assert error.value.code == 403


def test_missing_origin_and_duplicate_length_are_rejected(local_control):
    url, server, controller, started = local_control
    for duplicate in (False, True):
        connection = HTTPConnection("127.0.0.1", server.server_port, timeout=2)
        connection.putrequest("POST", "/api/control")
        connection.putheader("X-Revmamad-Token", controller.token)
        connection.putheader("Content-Type", "application/json")
        if duplicate:
            connection.putheader("Origin", url)
        connection.putheader("Content-Length", "18")
        if duplicate:
            connection.putheader("Content-Length", "18")
        connection.endheaders(b'{"action":"start"}')
        response = connection.getresponse()
        assert response.status == (413 if duplicate else 403)
        response.read()
        connection.close()
    assert not started.is_set()


def test_controls_are_off_by_default_and_cannot_bind_publicly(tmp_path):
    controller = ScanController(AppConfig.load(), lambda *args: None)
    for host in ("0.0.0.0", "192.168.1.1", "localhost"):
        with pytest.raises(ValueError, match="loopback"):
            create_server(host, 0, tmp_path, controller=controller)
    server = create_server("127.0.0.1", 0, tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        with urlopen(url + "/api/control", timeout=2) as response:
            assert json.load(response) == {"enabled": False, "job": None}
        with pytest.raises(HTTPError) as error:
            urlopen(request(url, controller, {"action": "start"}), timeout=2)
        assert error.value.code == 404
    finally:
        controller.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
