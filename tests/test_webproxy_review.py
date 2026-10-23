"""Independent regressions for WEB address identity and carrier cleanup."""

import asyncio
import base64
import hashlib

import pytest

from telegram_webproxy import TelegramWebProxy, parse_line
from telegram_webproxy.carrier import WebProxyCarrier, _WebSocket

SECRET = "000102030405060708090a0b0c0d0e0f"


@pytest.mark.parametrize(
    "address",
    [
        "127.1",
        "0x7f.0.0.1",
        "0177.0.0.1",
        "1.2.3.004",
        "127。0。0。1",
        "127．0．0．1",
        "example.123",
        "example.0x",
        "example.0x1f",
    ],
)
def test_numeric_hosts_cannot_change_identity_in_browser(address):
    with pytest.raises(ValueError, match="DNS hostname"):
        TelegramWebProxy(address, SECRET)
    assert parse_line(f"https://t.me/webproxy?server={address}&secret={SECRET}") is None


def test_web_hostname_uses_browser_nontransitional_idna():
    proxy = TelegramWebProxy("faß.de/Path/Sub", SECRET)
    assert proxy.hostname == "xn--fa-hia.de"
    assert proxy.base_path == "Path/Sub"
    canonical = TelegramWebProxy("xn--fa-hia.de/Path/Sub", SECRET)
    assert proxy.key() == canonical.key()
    assert proxy.bridge_capability == canonical.bridge_capability
    assert parse_line(proxy.web_link()).key() == proxy.key()
    assert TelegramWebProxy("ExAmPle。COM", SECRET).hostname == "example.com"


@pytest.mark.parametrize("address", ["xn--a.com", "xn--.com", "example。com。"])
def test_invalid_alabels_and_idna_trailing_dot_rejected(address):
    with pytest.raises(ValueError):
        TelegramWebProxy(address, SECRET)


class _Wire:
    def __init__(self, *, slow_close=False):
        self.output = bytearray()
        self.closed = False
        self.slow_close = slow_close

    def write(self, data):
        self.output.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        if self.slow_close:
            await asyncio.Event().wait()


@pytest.mark.parametrize("reason", ["WebSocket Protocol Handshake", "", "Custom\tUpgrade"])
@pytest.mark.asyncio
async def test_websocket_status_reason_is_not_part_of_upgrade_proof(monkeypatch, reason):
    proxy = TelegramWebProxy("proxy.example.com", SECRET)
    reader, wire = asyncio.StreamReader(), _Wire()
    nonce = b"x" * 16
    key = base64.b64encode(nonce).decode()
    protocol = "tproxy-v1." + "A" * 43
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode()
    reader.feed_data(
        (
            f"HTTP/1.1 101 {reason}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: {protocol}\r\n\r\n"
        ).encode()
    )

    async def connection(*args, **kwargs):
        return reader, wire

    monkeypatch.setattr(asyncio, "open_connection", connection)
    monkeypatch.setattr("telegram_webproxy.carrier.os.urandom", lambda size: nonce)
    ws = await _WebSocket.connect(proxy, protocol)
    await ws.close()
    assert wire.closed


@pytest.mark.asyncio
async def test_transport_wait_timeout_cannot_cancel_carrier_cleanup():
    carrier = WebProxyCarrier(TelegramWebProxy("proxy.example.com", SECRET))
    wire = _Wire(slow_close=True)
    carrier.ws = _WebSocket(asyncio.StreamReader(), wire)
    carrier.token = "A" * 43

    async def delete(*args, **kwargs):
        # DELETE plus the TLS shutdown can exceed the caller's 0.5s wait.
        await asyncio.sleep(0.12)
        return 204, {}, b""

    carrier._request = delete
    carrier.close()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(carrier.writer.wait_closed(), 0.5)
    assert not carrier.cleanup.cancelled()
    await asyncio.wait_for(carrier.wait_closed(), 1)
    assert carrier.client.is_closed
    assert wire.closed
    assert carrier.cleanup.done()


@pytest.mark.asyncio
async def test_direct_cleanup_cancellation_still_closes_http_client():
    carrier = WebProxyCarrier(TelegramWebProxy("proxy.example.com", SECRET))
    ws_started = asyncio.Event()

    class PausedSocket:
        async def close(self):
            ws_started.set()
            await asyncio.Event().wait()

    carrier.ws = PausedSocket()
    carrier.close()
    await ws_started.wait()
    carrier.cleanup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await carrier.cleanup
    assert carrier.client.is_closed
    assert carrier.reader.at_eof()


@pytest.mark.asyncio
async def test_cleanup_stops_waiting_for_stuck_socket():
    carrier = WebProxyCarrier(TelegramWebProxy("proxy.example.com", SECRET))

    class StuckSocket:
        async def close(self):
            await asyncio.Event().wait()

    carrier.ws = StuckSocket()
    carrier.close()
    await asyncio.wait_for(carrier.wait_closed(), 2.5)
    assert carrier.cleanup.done()
    assert carrier.client.is_closed
