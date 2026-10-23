"""Certified native HTTPS/WSS carrier for Telegram WEB relay protocol v1.

Each connection owns one relay session and stream. This keeps test retries/DCs
independent. Bridge JavaScript is never executed. Unlike an official app's
private WebView, Python/OpenSSL has its own TLS fingerprint.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import ssl
import time

import httpx

from telegram_webproxy.frames import (
    BYE,
    CLOSE,
    DATA,
    DATA_CHUNK,
    HELLO,
    INITIAL_WINDOW,
    MAX_BATCH,
    OPEN,
    PING,
    PONG,
    WINDOW,
    decode,
    encode,
)
from telegram_webproxy.models import TelegramWebProxy

_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_LITERAL = r'"(?:[^"\\\r\n]|\\["\\/bfnrt]|\\u[0-9a-fA-F]{4})*"'
_BRIDGE = re.compile(
    r"const\s+relayBase=(" + _LITERAL + r"),bootstrap=(" + _LITERAL + r"),carrierMode=(" + _LITERAL + r");"
)
_MODES = {"https", "https-lanes", "websocket", "websocket-lanes"}
_QUEUE_LIMIT = 8 * 1024 * 1024


def _token(value: str) -> str:
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        raise ValueError("Malformed WEB capability token")
    raw = base64.urlsafe_b64decode(value + "=")
    if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
        raise ValueError("Noncanonical WEB capability token")
    return value


def parse_bridge(body: bytes, proxy: TelegramWebProxy) -> tuple[str, str]:
    """Read reference bridge's literal configuration, without running scripts."""
    if len(body) > 2 * 1024 * 1024:
        raise ValueError("WEB bridge document is too large")
    try:
        matches = list(_BRIDGE.finditer(body.decode("utf-8", errors="strict")))
        if len(matches) != 1:
            raise ValueError("Unsupported WEB bridge document")
        relay_base, bootstrap, mode = (json.loads(value) for value in matches[0].groups())
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed WEB bridge document") from exc
    if relay_base != proxy.relay_base or mode not in _MODES:
        raise ValueError("WEB bridge origin/path or carrier mismatch")
    return _token(bootstrap), mode


class _WebSocket:
    """Small RFC 6455 binary client; no compression, cookies or redirects."""

    def __init__(self, reader, writer):
        self.reader, self.writer = reader, writer
        self.lock = asyncio.Lock()

    @classmethod
    async def connect(cls, proxy: TelegramWebProxy, protocol: str):
        reader, writer = await asyncio.open_connection(
            proxy.hostname, 443, ssl=ssl.create_default_context(), server_hostname=proxy.hostname, limit=16384
        )
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            path = "/" + (proxy.base_path + "/" if proxy.base_path else "") + "api/v1/ws"
            request = (
                f"GET {path} HTTP/1.1\r\nHost: {proxy.hostname}\r\nOrigin: {proxy.origin}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Protocol: {protocol}\r\n\r\n"
            )
            writer.write(request.encode("ascii"))
            await writer.drain()
            header = await reader.readuntil(b"\r\n\r\n")
            if len(header) > 16384:
                raise ValueError("Oversized WEB WebSocket response")
            lines = header.decode("ascii").split("\r\n")
            # RFC 9112 leaves the reason phrase arbitrary; the status code
            # and authenticated upgrade headers establish the handshake.
            if not re.fullmatch(r"HTTP/1\.1 101 [\t\x20-\x7e]*", lines[0]):
                raise ValueError("WEB WebSocket upgrade rejected")
            headers = {}
            for line in lines[1:-2]:
                name, value = line.split(":", 1)
                name = name.lower()
                if name in headers:
                    raise ValueError("Duplicate WEB WebSocket header")
                headers[name] = value.strip()
            expected = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            if (
                headers.get("sec-websocket-accept") != expected
                or headers.get("sec-websocket-protocol") != protocol
                or headers.get("upgrade", "").lower() != "websocket"
                or "upgrade"
                not in [item.strip().lower() for item in headers.get("connection", "").split(",")]
                or "sec-websocket-extensions" in headers
            ):
                raise ValueError("Invalid WEB WebSocket upgrade proof")
            return cls(reader, writer)
        except BaseException:
            writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 0.5)
            raise

    async def send(self, payload: bytes, opcode: int = 2):
        if len(payload) > MAX_BATCH or (opcode >= 8 and len(payload) > 125):
            raise ValueError("Oversized WEB WebSocket message")
        mask = os.urandom(4)
        size = len(payload)
        length = (
            bytes([0x80 | size])
            if size < 126
            else (b"\xfe" + size.to_bytes(2, "big") if size <= 65535 else b"\xff" + size.to_bytes(8, "big"))
        )
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        async with self.lock:
            self.writer.write(bytes([0x80 | opcode]) + length + mask + masked)
            await self.writer.drain()

    async def receive(self) -> bytes:
        chunks, size, fragmented = [], 0, False
        while True:
            first, second = await self.reader.readexactly(2)
            final, opcode = bool(first & 0x80), first & 15
            if first & 0x70 or second & 0x80:
                raise ValueError("Invalid WEB WebSocket flags")
            length = second & 127
            if length == 126:
                length = int.from_bytes(await self.reader.readexactly(2), "big")
                if length < 126:
                    raise ValueError("Noncanonical WebSocket length")
            elif length == 127:
                length = int.from_bytes(await self.reader.readexactly(8), "big")
                if length <= 65535:
                    raise ValueError("Noncanonical WebSocket length")
            if length > MAX_BATCH or (opcode >= 8 and (not final or length > 125)):
                raise ValueError("Oversized or fragmented WebSocket control")
            payload = await self.reader.readexactly(length)
            if opcode == 8:
                raise OSError("WEB WebSocket closed")
            if opcode == 9:
                await self.send(payload, 10)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 2) or (opcode == 0) != fragmented:
                raise ValueError("WEB requires ordered binary WebSocket messages")
            size += length
            if size > MAX_BATCH:
                raise ValueError("Oversized fragmented WEB message")
            chunks.append(payload)
            if final:
                return b"".join(chunks)
            fragmented = True

    async def close(self):
        self.writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.wait_closed(), 0.5)


class _Reader(asyncio.StreamReader):
    def __init__(self, carrier):
        super().__init__(limit=INITIAL_WINDOW)
        self.carrier = carrier

    async def read(self, n=-1):
        if n < 0:
            parts = []
            while True:
                part = await self.read(DATA_CHUNK)
                if not part:
                    return b"".join(parts)
                parts.append(part)
        data = await super().read(n)
        self.carrier.consumed(len(data))
        return data

    async def readexactly(self, n):
        if n < 0:
            raise ValueError("readexactly size cannot be negative")
        parts, remaining = [], n
        while remaining:
            part = await self.read(min(remaining, DATA_CHUNK))
            if not part:
                raise asyncio.IncompleteReadError(b"".join(parts), n)
            parts.append(part)
            remaining -= len(part)
        return b"".join(parts)


class _Writer:
    def __init__(self, carrier):
        self.carrier = carrier

    def write(self, data):
        self.carrier.write(data)

    async def drain(self):
        await self.carrier.drain()

    def close(self):
        self.carrier.close()

    async def wait_closed(self):
        await self.carrier.wait_closed()

    def is_closing(self):
        return self.carrier.closing

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return (self.carrier.proxy.hostname, 443)
        if name == "telegram_webproxy_carrier":
            return self.carrier.mode
        return default


class WebProxyCarrier:
    """One bounded logical stream over any reference relay carrier mode."""

    def __init__(self, proxy: TelegramWebProxy):
        self.proxy = proxy
        self.client = httpx.AsyncClient(verify=True, trust_env=False, timeout=35, follow_redirects=False)
        self.reader = _Reader(self)
        self.writer = _Writer(self)
        self.mode, self.token, self.ws = "", "", None
        self.credit = self.receive_credit = INITIAL_WINDOW
        self.pending = bytearray()
        self.window_pending = 0
        self.up_seq, self.down_cursor = 1, 0
        self.closing, self.error = False, None
        self.wake = asyncio.Event()
        self.send_lock = asyncio.Lock()
        self.drained = asyncio.Event()
        self.drained.set()
        self.tasks = []
        self.cleanup = None

    async def _request(self, method, endpoint, token="", body=None, extra=None, max_size=MAX_BATCH):
        headers = {"Origin": self.proxy.origin, "Cache-Control": "no-store"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if body is not None:
            headers["Content-Type"] = "application/octet-stream"
        headers.update(extra or {})
        request = self.client.build_request(method, endpoint, headers=headers, content=body)
        request.headers.pop("cookie", None)
        response = await self.client.send(request, stream=True, follow_redirects=False)
        try:
            parts, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > max_size:
                    raise ValueError("Oversized WEB HTTP body")
                parts.append(chunk)
            return response.status_code, response.headers, b"".join(parts)
        finally:
            await response.aclose()

    async def start(self):
        status, headers, body = await self._request("GET", self.proxy.bridge_url, max_size=2 * 1024 * 1024)
        if status != 200 or headers.get("content-type", "").split(";")[0].strip().lower() != "text/html":
            raise ValueError("WEB bridge was not served")
        bootstrap, self.mode = parse_bridge(body, self.proxy)
        started = time.monotonic()
        while True:
            status, headers, body = await self._request(
                "POST",
                self.proxy.relay_base + "api/v1/session",
                bootstrap,
                encode(HELLO, payload=b"\x01"),
                max_size=64,
            )
            if status != 503:
                break
            if time.monotonic() - started > 90:
                raise TimeoutError("WEB session capacity timeout")
            await asyncio.sleep(self._retry_after(headers))
        if status != 200 or headers.get("X-Carrier-Mode") != self.mode or headers.get("X-Down-Cursor") != "0":
            raise ValueError("WEB session creation rejected")
        decode(body, welcome=True)
        self.token = _token(headers.get("X-Session-Token", ""))
        if self.mode.startswith("websocket"):
            protocol = (
                ("tproxy-lane-v1." + self.token + ".1")
                if self.mode.endswith("lanes")
                else ("tproxy-v1." + self.token)
            )
            self.ws = await _WebSocket.connect(self.proxy, protocol)
        await self._send(encode(OPEN, 1))
        self.tasks = [asyncio.create_task(self._receive_loop()), asyncio.create_task(self._send_loop())]
        return self.reader, self.writer

    @staticmethod
    def _retry_after(headers):
        value = headers.get("Retry-After", "1")
        if not re.fullmatch(r"[0-9]{1,2}", value) or not 1 <= int(value) <= 30:
            raise ValueError("Invalid WEB Retry-After")
        return int(value)

    async def _send(self, batch):
        async with self.send_lock:
            await self._send_locked(batch)

    async def _send_locked(self, batch):
        if self.ws is not None:
            await self.ws.send(batch)
            return
        extra = {"X-Up-Seq": str(self.up_seq)}
        if self.mode.endswith("lanes"):
            extra["X-Lane-ID"] = "1"
        started = time.monotonic()
        while True:
            status, headers, body = await self._request(
                "POST", self.proxy.relay_base + "api/v1/up", self.token, batch, extra, max_size=64
            )
            if status != 503:
                break
            if time.monotonic() - started > 90:
                raise TimeoutError("WEB uplink capacity timeout")
            await asyncio.sleep(self._retry_after(headers))
        if status != 204 or body or headers.get("X-Up-Ack") != str(self.up_seq):
            raise ValueError("WEB uplink acknowledgement rejected")
        self.up_seq += 1

    async def _receive_loop(self):
        try:
            while not self.closing:
                if self.ws is not None:
                    body = await self.ws.receive()
                else:
                    extra = {"X-Down-Cursor": str(self.down_cursor)}
                    if self.mode.endswith("lanes"):
                        extra["X-Lane-ID"] = "1"
                    status, headers, body = await self._request(
                        "POST", self.proxy.relay_base + "api/v1/down", self.token, extra=extra
                    )
                    cursor = headers.get("X-Down-Cursor", "")
                    expected = self.down_cursor + (1 if status == 200 else 0)
                    if status not in (200, 204) or cursor != str(expected) or (status == 204 and body):
                        raise ValueError("WEB downlink cursor rejected")
                    self.down_cursor = expected
                    if headers.get("X-Lane-Closed") == "1":
                        self._eof()
                        return
                    if status == 204:
                        await asyncio.sleep(0.01)
                        continue
                for frame in decode(body):
                    if frame.stream == 0:
                        if self.mode.endswith("lanes") or frame.type != PING:
                            raise ValueError("Unexpected WEB session control")
                        await self._send(encode(PONG, payload=frame.payload))
                        continue
                    if frame.stream != 1:
                        raise ValueError("Cross-stream WEB data")
                    if frame.type == DATA:
                        if len(frame.payload) > self.receive_credit:
                            raise ValueError("WEB receive window exhausted")
                        self.receive_credit -= len(frame.payload)
                        self.reader.feed_data(frame.payload)
                    elif frame.type == WINDOW:
                        amount = int.from_bytes(frame.payload, "big")
                        if self.credit + amount > INITIAL_WINDOW:
                            raise ValueError("WEB send window overflow")
                        self.credit += amount
                        self.wake.set()
                    elif frame.type == CLOSE:
                        self._eof()
                        return
                    elif frame.type == BYE:
                        raise OSError("WEB relay session ended")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc)

    async def _send_loop(self):
        try:
            while not self.closing:
                await self.wake.wait()
                self.wake.clear()
                if self.window_pending:
                    amount, self.window_pending = self.window_pending, 0
                    await self._send(encode(WINDOW, 1, amount.to_bytes(4, "big")))
                while self.pending and self.credit and not self.closing:
                    size = min(len(self.pending), self.credit, DATA_CHUNK)
                    data = bytes(self.pending[:size])
                    del self.pending[:size]
                    self.credit -= size
                    await self._send(encode(DATA, 1, data))
                if not self.pending:
                    self.drained.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._fail(exc)

    def consumed(self, size):
        if size and not self.closing:
            self.receive_credit += size
            self.window_pending += size
            self.wake.set()

    def write(self, data):
        if self.closing:
            raise OSError("WEB stream is closed") from self.error
        if (
            not isinstance(data, (bytes, bytearray, memoryview))
            or len(data) + len(self.pending) > _QUEUE_LIMIT
        ):
            raise ValueError("WEB writer queue limit exceeded")
        self.pending.extend(data)
        if data:
            self.drained.clear()
            self.wake.set()

    async def drain(self):
        await self.drained.wait()
        if self.error:
            raise OSError("WEB carrier failed") from self.error
        if self.closing:
            raise OSError("WEB stream closed")

    def _eof(self):
        # Shared CLOSE aborts queued data, it is not a TCP half-close.
        self.reader._buffer.clear()
        self.reader.feed_eof()
        self.close()

    def _fail(self, error):
        if not self.closing:
            self.error = error
            self.reader.set_exception(OSError("WEB carrier protocol or network failure"))
            self.close()

    def close(self):
        if not self.closing:
            self.closing = True
            self.pending.clear()
            self.drained.set()
            self.cleanup = asyncio.create_task(self._close())

    async def _close(self):
        try:
            # The transport caller has a short wait_closed budget. Cleanup
            # may outlive that waiter, but all its network work is bounded.
            async with asyncio.timeout(1.5):
                for task in self.tasks:
                    if task is not asyncio.current_task():
                        task.cancel()
                await asyncio.gather(*self.tasks, return_exceptions=True)
                if self.token:
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            self._request(
                                "DELETE", self.proxy.relay_base + "api/v1/session", self.token, max_size=64
                            ),
                            0.2,
                        )
                if self.ws is not None:
                    await self.ws.close()
        except TimeoutError:
            pass
        finally:
            try:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(0.5):
                        await self.client.aclose()
            finally:
                if not self.reader.at_eof():
                    self.reader.feed_eof()

    async def wait_closed(self):
        if self.cleanup is not None:
            # A caller's timeout must not cancel resource cleanup halfway
            # through DELETE or a TLS close and leave pooled HTTP sockets.
            await asyncio.shield(self.cleanup)


async def open_connection(proxy: TelegramWebProxy, *, timeout: float = 12.0):
    """Return an asyncio reader/writer after authenticated carrier bootstrap.

    This operation proves only a relay carrier. The caller must perform the
    ordinary MTProxy transform and strict authenticated Telegram session check.
    """
    carrier = WebProxyCarrier(proxy)
    try:
        async with asyncio.timeout(timeout):
            return await carrier.start()
    except BaseException:
        carrier.close()
        with contextlib.suppress(Exception):
            await asyncio.shield(carrier.wait_closed())
        raise
