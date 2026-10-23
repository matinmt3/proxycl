"""A dashboard using Python's standard library, including in Termux."""

from __future__ import annotations

import ipaddress
import json
import logging
import secrets
import shutil
import subprocess
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dashboard import data
from dashboard.control import MAX_BODY, ControlError

logger = logging.getLogger("mtselector.dashboard")
ASSETS = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, output_folder: Path, controller=None, **kwargs):
        self.output_folder = output_folder
        self.controller = controller
        super().__init__(*args, **kwargs)

    def _json(self, status, body):
        # Drain only a small, consistently declared body before rejecting it.
        # Closing a Windows socket with unread request bytes can discard the
        # error response (TCP reset). This never parses or launches a request.
        if self.command == "POST" and not getattr(self, "_body_consumed", False):
            lengths = self.headers.get_all("Content-Length", [])
            if (
                lengths
                and len(set(lengths)) == 1
                and len(lengths[0]) <= 5
                and lengths[0].isascii()
                and lengths[0].isdigit()
            ):
                amount = int(lengths[0])
                if 0 < amount <= 65536 and self.headers.get("Transfer-Encoding") is None:
                    previous_timeout = self.connection.gettimeout()
                    try:
                        self.connection.settimeout(0.25)
                        self.rfile.read(amount)
                    except OSError:
                        pass
                    finally:
                        self.connection.settimeout(previous_timeout)
            self._body_consumed = True
        content = json.dumps(body, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _control_origin(self, *, post=False):
        """Exact bound authority prevents DNS rebinding; no CORS or URL tokens."""
        host, port = self.server.server_address[:2]
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        origin = f"http://{authority}"
        return (
            self.headers.get_all("Host") == [authority]
            and (
                self.headers.get_all("Origin") == [origin]
                if post
                else self.headers.get_all("Origin") in (None, [origin])
            )
            and self.headers.get("Sec-Fetch-Site") in (None, "same-origin", "none")
            and ipaddress.ip_address(self.client_address[0]).is_loopback
        )

    def do_POST(self):
        if self.path != "/api/control" or self.controller is None:
            self._json(404, {"error": "Controls are unavailable"})
            return
        if not self._control_origin(post=True):
            self._json(403, {"error": "Use this dashboard's local address"})
            return
        tokens = self.headers.get_all("X-Revmamad-Token", [])
        if len(tokens) != 1 or not secrets.compare_digest(
            tokens[0].encode("utf-8"), self.controller.token.encode("ascii")
        ):
            self._json(403, {"error": "Reload the dashboard to refresh its local control token"})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (
            len(lengths) != 1
            or len(lengths[0]) > 4
            or not lengths[0].isascii()
            or not lengths[0].isdigit()
            or not 0 < int(lengths[0]) <= MAX_BODY
            or self.headers.get("Transfer-Encoding") is not None
        ):
            self._json(413, {"error": "Scan request is too large or has no valid length"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            self._json(415, {"error": "Send a JSON scan request"})
            return
        try:

            def unique_keys(pairs):
                row = {}
                for key, value in pairs:
                    if key in row:
                        raise ValueError("Repeated option")
                    row[key] = value
                return row

            self.connection.settimeout(5)
            self._body_consumed = True
            payload = json.loads(self.rfile.read(int(lengths[0])), object_pairs_hook=unique_keys)
            job = self.controller.request(payload)
        except ControlError as exc:
            self._json(exc.status, {"error": str(exc)})
        except (ValueError, UnicodeError, RecursionError, TimeoutError, OSError):
            self._json(400, {"error": "Invalid JSON scan request"})
        else:
            self._json(202, {"job": job})

    def do_GET(self):
        request = urlsplit(self.path)
        try:
            if request.path == "/api/control":
                if request.query or (self.controller is not None and not self._control_origin()):
                    self._json(403, {"error": "Use this dashboard's local address"})
                else:
                    self._json(
                        200,
                        (
                            {**self.controller.status(), "token": self.controller.token}
                            if self.controller
                            else {"enabled": False, "job": None}
                        ),
                    )
                return
            if request.path in ("/", "/dashboard.js", "/styles.css"):
                filename = "index.html" if request.path == "/" else request.path[1:]
                content = (ASSETS / filename).read_bytes()
                content_type = {
                    "index.html": "text/html",
                    "dashboard.js": "application/javascript",
                    "styles.css": "text/css",
                }[filename]
            elif request.path in ("/api/snapshot", "/api/stats", "/api/proxies"):
                content = json.dumps(
                    data.api_response(
                        request.path, self.output_folder, parse_qs(request.query, keep_blank_values=True)
                    ),
                    allow_nan=False,
                ).encode("utf-8")
                content_type = "application/json"
            else:
                self.send_error(404)
                return
        except (ValueError, TypeError):
            self.send_error(400, "Invalid query parameters")
            return
        except OSError:
            self.send_error(503, "Dashboard asset unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        logger.debug(format, *args)


def create_server(host: str, port: int, output_folder: str | Path, controller=None) -> ThreadingHTTPServer:
    if controller is not None:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("Dashboard scan controls require a loopback address such as 127.0.0.1")
    server = ThreadingHTTPServer(
        (host, port), partial(Handler, output_folder=Path(output_folder), controller=controller)
    )
    server.daemon_threads = True
    return server


def open_dashboard(url: str) -> bool:
    """Use Android's URL intent when available, otherwise the desktop browser."""
    try:
        termux_opener = shutil.which("termux-open-url")
        if termux_opener:
            subprocess.Popen([termux_opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        return bool(webbrowser.open(url))
    except (OSError, webbrowser.Error):
        logger.warning("Could not open the browser automatically. Open %s manually.", url)
        return False


def run(host: str, port: int, output_folder: str | Path, open_browser: bool = True, controller=None):
    with create_server(host, port, output_folder, controller=controller) as server:
        browser_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        if ":" in browser_host:
            browser_host = f"[{browser_host}]"
        url = f"http://{browser_host}:{server.server_port}"
        print(f"REVMAMAD dashboard: {url}\nKeep this session running. Press Ctrl+C to return.", flush=True)
        if open_browser and not open_dashboard(url):
            print(f"Open this address in your phone's browser: {url}", flush=True)
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            pass
        finally:
            if controller is not None:
                controller.close()
