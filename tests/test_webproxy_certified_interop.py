"""Opt-in official relay integration with real local TLS, not Telegram/device proof.

REVMAMAD_RELAY_FIXTURE is a built tproxy-server executable at the pinned commit.
REVMAMAD_OPENSSL names openssl for a short-lived test CA; no trust store changes.
The historical Windows binary only overlays Unix permission-bit checks. That
binary is external to the product. Default pytest runs skip this external check.
"""

import asyncio
import contextlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from telegram_webproxy.carrier import WebProxyCarrier
from telegram_webproxy.models import TelegramWebProxy

PINNED_COMMIT = "c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8"
RELAY = os.environ.get("REVMAMAD_RELAY_FIXTURE")
OPENSSL = os.environ.get("REVMAMAD_OPENSSL") or shutil.which("openssl")
pytestmark = pytest.mark.skipif(
    not RELAY or not OPENSSL, reason="Explicit official relay binary/openssl needed"
)


def available_port():
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return connection.getsockname()[1]


class CertifiedLocalTransport(httpx.AsyncBaseTransport):
    def __init__(self, port, context):
        self.port = port
        self.transport = httpx.AsyncHTTPTransport(verify=context, trust_env=False)

    async def handle_async_request(self, request):
        # HTTPX connects to loopback, while TLS authenticates the actual relay
        # hostname. Host/Origin/capability/protocol contents stay unmodified.
        url = request.url.copy_with(host="127.0.0.1", port=self.port)
        redirected = httpx.Request(
            request.method,
            url,
            headers=request.headers,
            content=await request.aread(),
            extensions={**request.extensions, "sni_hostname": "proxy.example.com"},
        )
        return await self.transport.handle_async_request(redirected)

    async def aclose(self):
        await self.transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["https", "https-lanes", "websocket", "websocket-lanes"])
@pytest.mark.parametrize("base_path", ["", "Path/Sub"])
async def test_official_relay_certified_flow_control_and_reconnection(tmp_path, monkeypatch, mode, base_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            OPENSSL,
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-noenc",
            "-days",
            "1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-subj",
            "/CN=proxy.example.com",
            "-addext",
            "subjectAltName=DNS:proxy.example.com",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
    )
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    original_context = ssl.create_default_context
    client_context = original_context(cafile=str(cert))
    assert client_context.check_hostname and client_context.verify_mode == ssl.CERT_REQUIRED
    seen_sni = []
    server_context.set_servername_callback(lambda sock, hostname, ctx: seen_sni.append(hostname))
    original_connect = asyncio.open_connection
    backend_clients, front_clients, tasks = set(), set(), set()

    async def echo(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        backend_clients.add(writer)
        try:
            while body := await reader.read(65536):
                writer.write(body)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            backend_clients.discard(writer)
            tasks.discard(task)

    relay_port, admin_port = available_port(), available_port()
    backend = await asyncio.start_server(echo, "127.0.0.1", 0)
    backend_port = backend.sockets[0].getsockname()[1]

    async def front(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        front_clients.add(writer)
        remote = None
        pumps = []
        try:
            upstream, remote = await original_connect("127.0.0.1", relay_port)

            async def pump(source, destination):
                while body := await source.read(65536):
                    destination.write(body)
                    await destination.drain()

            pumps = [asyncio.create_task(pump(reader, remote)), asyncio.create_task(pump(upstream, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            for pump_task in pumps:
                pump_task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for connection in (writer, remote):
                if connection:
                    connection.close()
                    with contextlib.suppress(Exception):
                        await connection.wait_closed()
            front_clients.discard(writer)
            tasks.discard(task)

    front_server = await asyncio.start_server(front, "127.0.0.1", 0, ssl=server_context)
    front_port = front_server.sockets[0].getsockname()[1]
    public = tmp_path / "public"
    public.mkdir()
    (public / "index.html").write_text("fixture")
    secret = "dd000102030405060708090a0b0c0d0e0f"
    (tmp_path / "profiles.json").write_text(
        json.dumps(
            {
                "profiles": [
                    {
                        "name": "fixture",
                        "secret": secret,
                        "backend": f"127.0.0.1:{backend_port}",
                        "carrier_mode": mode,
                    }
                ]
            }
        )
    )
    (tmp_path / "token.key").write_bytes(os.urandom(32))
    for path in (tmp_path / "profiles.json", tmp_path / "token.key"):
        path.chmod(0o600)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "public_hostname": "proxy.example.com",
                "base_path": base_path,
                "public_dir": "public",
                "profiles_file": "profiles.json",
                "listen": f"127.0.0.1:{relay_port}",
                "admin_listen": f"127.0.0.1:{admin_port}",
            }
        )
    )
    log = (tmp_path / "server.log").open("wb")
    process = subprocess.Popen(
        [RELAY, "-config", str(tmp_path / "config.json")],
        stdout=log,
        stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    carriers = []
    started = time.monotonic()
    try:
        async with asyncio.timeout(30):
            for _ in range(60):
                if process.poll() is not None:
                    raise RuntimeError("Official relay exited: " + (tmp_path / "server.log").read_text())
                try:
                    _, writer = await original_connect("127.0.0.1", relay_port)
                    writer.close()
                    await writer.wait_closed()
                    break
                except OSError:
                    await asyncio.sleep(0.05)
            # This CA is not in default trust, and trusting it still enforces
            # hostname matching. Both failures happen before HTTP/proxy traffic.
            with pytest.raises(ssl.SSLCertVerificationError):
                await original_connect(
                    "127.0.0.1", front_port, ssl=original_context(), server_hostname="proxy.example.com"
                )
            with pytest.raises(ssl.SSLCertVerificationError):
                await original_connect(
                    "127.0.0.1", front_port, ssl=client_context, server_hostname="wrong.example.com"
                )

            async def redirected_connect(host, port, **kwargs):
                assert host == "proxy.example.com" and port == 443
                assert kwargs["ssl"].check_hostname and kwargs["server_hostname"] == host
                return await original_connect("127.0.0.1", front_port, **kwargs)

            monkeypatch.setattr(asyncio, "open_connection", redirected_connect)
            monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: client_context)
            proxy = TelegramWebProxy("proxy.example.com" + ("/" + base_path if base_path else ""), secret)
            payload = bytes(range(256)) * 20000  # 5,120,000 > 4MiB relay window
            for attempt in range(2):
                carrier = WebProxyCarrier(proxy)
                carriers.append(carrier)
                await carrier.client.aclose()
                carrier.client = httpx.AsyncClient(
                    transport=CertifiedLocalTransport(front_port, client_context), trust_env=False
                )
                reader, writer = await carrier.start()
                writer.write(payload)
                _, actual = await asyncio.gather(writer.drain(), reader.readexactly(len(payload)))
                assert actual == payload
                if attempt == 0:
                    for active in list(front_clients):
                        active.transport.abort()
                    with pytest.raises((OSError, asyncio.IncompleteReadError)):
                        await asyncio.wait_for(reader.readexactly(1), 3)
                writer.close()
                await writer.wait_closed()
            assert all(name in {"proxy.example.com", "wrong.example.com"} for name in seen_sni)
            report = {
                "profile": mode,
                "base_path": base_path,
                "official_commit": PINNED_COMMIT,
                "verified_tls": True,
                "untrusted_ca_rejected": True,
                "wrong_hostname_rejected": True,
                "bytes_per_connection": len(payload),
                "fresh_connections": 2,
                "forced_drop_detected": True,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "scope": "local certified relay + echo; not Telegram/account/device proof",
            }
            destination = os.environ.get("REVMAMAD_WEB_CONFORMANCE_REPORT")
            if destination:
                path = Path(destination)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(report) + "\n")
    finally:
        for carrier in carriers:
            carrier.close()
        await asyncio.gather(*(carrier.wait_closed() for carrier in carriers), return_exceptions=True)
        process.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=3)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        log.close()
        front_server.close()
        backend.close()
        await front_server.wait_closed()
        await backend.wait_closed()
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
