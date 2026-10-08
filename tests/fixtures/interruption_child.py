"""A child process receives native SIGINT while real test sockets are pending."""

import asyncio
import json
import signal
import socketserver
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from utils.models import FailureReason, Proxy, TestSample
from webproxy.models import WebProxy

opened = closed = 0
waiting = set()
lock = threading.Lock()
loop = None
empty = len(sys.argv) > 2 and sys.argv[2] == "--empty"


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        global opened, closed
        with lock:
            opened += 1
        try:
            server, attempt = self.rfile.readline().decode().strip().split()
            if not empty and server == "completed.example":
                self.wfile.write(b"FAIL\n" if attempt == "1" else b"OK\n")
                self.wfile.flush()
            elif not empty and server == "partial.example" and attempt == "1":
                self.wfile.write(b"OK\n")
                self.wfile.flush()
            else:
                with lock:
                    waiting.add(server)
                    ready = waiting == (
                        {"completed.example", "partial.example"}
                        if empty
                        else {"partial.example", "untested.example"}
                    )
                if ready:
                    # Python's native signal.raise_signal is portable to Windows and
                    # invokes the same SIGINT handler installed by asyncio.Runner.
                    loop.call_soon_threadsafe(signal.raise_signal, signal.SIGINT)
                self.rfile.read(1)
        finally:
            with lock:
                closed += 1


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True


server = Server(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
thread.start()
calls = {}


async def network_probe(proxy, *args, **kwargs):
    global loop
    loop = asyncio.get_running_loop()
    calls[proxy.server] = calls.get(proxy.server, 0) + 1
    reader, writer = await asyncio.open_connection("127.0.0.1", server.server_address[1])
    try:
        writer.write(f"{proxy.server} {calls[proxy.server]}\n".encode())
        await writer.drain()
        answer = await reader.readline()
        if answer == b"OK\n":
            return TestSample(True, total_latency_ms=10)
        return TestSample(False, failure_reason=FailureReason.HANDSHAKE_INVALID)
    finally:
        writer.close()
        await writer.wait_closed()


config = SimpleNamespace(
    testing=SimpleNamespace(workers=2, retries=2, connect_timeout_seconds=30, timeout_seconds=30)
)
names = ["completed.example", "partial.example", "untested.example", "never-started.example"]
if sys.argv[1] == "web":
    import webproxy.tester as module

    tester = module.WebTester(config)
    proxies = [WebProxy(name, 8080, "http") for name in names]
else:
    import tester.runner as module

    tester = module.ParallelTester(config)
    proxies = [Proxy(name, 443, "00112233445566778899aabbccddeeff") for name in names]
module.test_proxy_once = network_probe
pipeline = len(sys.argv) > 2 and not empty
if pipeline:
    root = Path(sys.argv[2])
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / "config.yaml"
    config_path.write_text(
        "sources: []\nweb_sources: []\ntesting:\n  workers: 2\n  retries: 2\n"
        "  timeout_seconds: 30\n  connect_timeout_seconds: 30\n"
        "output:\n  folder: results\n  top_sizes: []\n",
        encoding="utf-8",
    )
    folder = root / "results" / sys.argv[1]
    folder.mkdir(parents=True)
    (folder / "completed_snapshot.json").write_bytes(b'{"prior":"last completed generation"}')
    if sys.argv[1] == "web":
        from webproxy.collector import WebCollector as Collector
    else:
        from collector.collector import Collector

    def collect(self):
        self.source_reports = [{"name": "local fixture", "status": "ok", "count": 4}]
        return proxies

    Collector.collect = collect
try:
    if pipeline:
        import main

        cli_exit = main.main(
            ["best10", "--mode", sys.argv[1], "--count", "ALL", "--config", str(config_path)]
        )
        payload = {"signal": "SIGINT", "cli_exit": cli_exit}
    else:
        tester.run(proxies)
except KeyboardInterrupt as interrupted:
    rows = [
        {"server": row.proxy.server, "attempts": row.attempts, "successes": row.successes}
        for row in getattr(interrupted, "results", [])
    ]
    payload = {"signal": "SIGINT", "exception": type(interrupted).__name__, "rows": rows}
else:
    if not pipeline:
        raise AssertionError("Native SIGINT was swallowed")
finally:
    deadline = time.monotonic() + 2
    while closed != opened and time.monotonic() < deadline:
        time.sleep(0.01)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)
payload.update({"opened": opened, "closed": closed})
print("REPORT:" + json.dumps(payload), flush=True)
