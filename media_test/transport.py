"""A Telethon Connection whose every socket stays on one immutable proxy route.

Telethon is imported only by connection_factory. Reconnection and media-DC
export create new connections from the same class/closure. CDN is rejected by
the client before a new client or route can be constructed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from media_test.models import TELETHON_VERSION, BudgetExceeded
from telegram_webproxy.models import TelegramWebProxy
from tester.faketls import authenticate_server_hello, build_client_hello
from tester.mtproto import _SessionTransport
from tester.secrets import decode_secret, normalize_port, normalize_server
from utils.models import Proxy

MEDIA_PACKET_MAX = 2 * 1024 * 1024


@dataclass
class TransferBudget:
    """Count observed obfuscated-stream bytes, not device-wide IP/TLS overhead.

    Reservations avoid concurrent read/write overshoot. A read is reserved
    before awaiting data and released on interruption; partial reads count.
    WEB HTTP headers, TLS and relay control frames are outside this layer.
    """

    limit: int
    consumed: int = 0
    reserved: int = 0
    exhausted: bool = False

    def reserve(self, count: int) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("Invalid byte count")
        if self.exhausted or self.consumed + self.reserved + count > self.limit:
            self.exhausted = True
            raise BudgetExceeded("media_budget_exhausted")
        self.reserved += count

    def finish(self, reserved: int, consumed: int) -> None:
        self.reserved -= reserved
        self.consumed += consumed

    def charge(self, count: int) -> None:
        self.reserve(count)
        self.finish(count, count)


class CountingReader:
    def __init__(self, reader, budget: TransferBudget):
        self.reader, self.budget = reader, budget

    async def readexactly(self, count: int) -> bytes:
        self.budget.reserve(count)
        consumed = 0
        chunks = []
        try:
            # Charge partial progress even if cancellation interrupts a frame.
            # readexactly() alone would hide buffered progress on cancellation.
            while consumed < count:
                chunk = await self.reader.read(min(count - consumed, 65536))
                if not chunk:
                    raise asyncio.IncompleteReadError(b"".join(chunks), count)
                chunks.append(chunk)
                consumed += len(chunk)
            return b"".join(chunks)
        finally:
            self.budget.finish(count, consumed)


class CountingWriter:
    def __init__(self, writer, budget: TransferBudget):
        self.writer, self.budget = writer, budget

    def write(self, data):
        self.budget.charge(len(data))
        self.writer.write(data)

    async def drain(self):
        await self.writer.drain()


@dataclass(frozen=True)
class PinnedRoute:
    server: str
    port: int
    secret: str
    web: bool

    @classmethod
    def from_proxy(cls, proxy):
        if not isinstance(proxy, (Proxy, TelegramWebProxy)):
            raise ValueError("Media test supports qualified MTProto or Telegram WEB only")
        secret = decode_secret(proxy.secret).encoded
        web = isinstance(proxy, TelegramWebProxy)
        server = proxy.server if web else normalize_server(proxy.server)
        return cls(server, normalize_port(proxy.port), secret, web)


def require_telethon():
    try:
        import telethon
    except ImportError:
        raise RuntimeError("Install optional dependencies: pip install -r requirements-media.txt") from None
    if telethon.__version__ != TELETHON_VERSION:
        raise RuntimeError(f"Media tests require the audited Telethon {TELETHON_VERSION}")
    return telethon


def connection_factory(proxy, budget: TransferBudget):
    require_telethon()
    from telethon.network.connection import Connection

    route = PinnedRoute.from_proxy(proxy)

    class PinnedConnection(Connection):
        """Atomic packet interface used by main, exported and reconnected senders."""

        def __init__(self, ip, port, dc_id, *, loggers, proxy=None, local_addr=None):
            # ip/port describe the Telegram DC, never a permitted socket target.
            super().__init__(ip, port, dc_id, loggers=loggers, proxy=None, local_addr=None)
            if type(dc_id) is not int or not 1 <= dc_id <= 5:
                raise ValueError("Unsupported Telegram data center")
            if proxy is not None or local_addr is not None:
                raise ValueError("Route override is not permitted")
            self._packet_transport = None
            self._send_lock = asyncio.Lock()
            self._receive_lock = asyncio.Lock()

        async def connect(self, timeout=None, ssl=None):
            if ssl is not None:
                raise ValueError("Additional route SSL override is not permitted")
            if self._connected:
                return
            secret = decode_secret(route.secret)
            try:
                async with asyncio.timeout(timeout or 15):
                    if route.web:
                        from telegram_webproxy.carrier import open_connection

                        selected = TelegramWebProxy(route.server, route.secret)
                        reader, writer = await open_connection(selected, timeout=timeout or 15)
                    else:
                        reader, writer = await asyncio.open_connection(route.server, route.port)
                    self._writer = writer
                    reader, counted_writer = CountingReader(reader, budget), CountingWriter(writer, budget)
                    if secret.domain:
                        hello = build_client_hello(secret)
                        counted_writer.write(hello)
                        await counted_writer.drain()
                        await authenticate_server_hello(reader, secret, hello)
                    self._packet_transport = _SessionTransport(
                        reader, counted_writer, secret, self._dc_id, maximum=MEDIA_PACKET_MAX
                    )
                    self._connected = True
            except BaseException:
                await self.disconnect()
                raise

        async def send(self, data):
            if not self._connected:
                raise ConnectionError("Media route disconnected")
            async with self._send_lock:
                await self._packet_transport.send(data)

        async def recv(self):
            if not self._connected:
                raise ConnectionError("Media route disconnected")
            async with self._receive_lock:
                try:
                    return await self._packet_transport.receive()
                except ValueError:
                    raise ConnectionError("Invalid media transport frame") from None

        async def disconnect(self):
            self._connected = False
            writer, self._writer = self._writer, None
            self._packet_transport = None
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), 0.5)
                except (OSError, TimeoutError):
                    pass

        def __str__(self):
            return "PinnedTelegramMediaRoute"

    return PinnedConnection
