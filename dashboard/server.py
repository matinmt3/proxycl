"""A dashboard using Python's standard library, including in Termux."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dashboard import data

logger = logging.getLogger("mtselector.dashboard")
ASSETS = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, data_file: Path, **kwargs):
        self.data_file = data_file
        super().__init__(*args, **kwargs)

    def do_GET(self):
        request = urlsplit(self.path)
        try:
            if request.path in ("/", "/dashboard.js"):
                filename = "index.html" if request.path == "/" else "dashboard.js"
                content = (ASSETS / filename).read_bytes()
                content_type = "text/html" if filename.endswith("html") else "application/javascript"
            elif request.path == "/api/stats":
                content = json.dumps(data.stats(self.data_file), allow_nan=False).encode("utf-8")
                content_type = "application/json"
            elif request.path == "/api/proxies":
                params = parse_qs(request.query)

                def value(key, default):
                    return params.get(key, [default])[0]

                content = json.dumps(
                    data.proxies(
                        self.data_file,
                        value("search", ""),
                        value("sort_by", "score"),
                        value("order", "desc"),
                        int(value("limit", "200")),
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
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format, *args):
        logger.debug(format, *args)


def create_server(host: str, port: int, output_folder: str | Path) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), partial(Handler, data_file=Path(output_folder) / "proxy.json"))
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


def run(host: str, port: int, output_folder: str | Path, open_browser: bool = True):
    with create_server(host, port, output_folder) as server:
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
