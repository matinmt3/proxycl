"""Protocol fixtures for tproxy-server c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8.

Fixtures cover the documented wire contract, not a real Telegram account/server.
"""

import asyncio
import base64
import json

import httpx
import pytest

from telegram_webproxy import TelegramWebProxy, extract_links, parse_line
from telegram_webproxy.carrier import (
    WebProxyCarrier,
    _Reader,
    _token,
    _WebSocket,
    open_connection,
    parse_bridge,
)
from telegram_webproxy.frames import (
    CLOSE,
    DATA,
    HELLO,
    INITIAL_WINDOW,
    OPEN,
    PING,
    WELCOME,
    WINDOW,
    decode,
    encode,
)

SECRET = "000102030405060708090a0b0c0d0e0f"
TOKEN = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()


def bridge(proxy, mode="websocket", token=TOKEN):
    return (
        "<!doctype html><script>const relayBase="
        + json.dumps(proxy.relay_base)
        + ",bootstrap="
        + json.dumps(token)
        + ",carrierMode="
        + json.dumps(mode)
        + ";</script>"
    ).encode()


@pytest.mark.parametrize(
    "path,secret,expected",
    [
        ("", SECRET, "MHLEY5PmW1GWqJkSrlmJpvJUiLhBH_QKy6yKg8a0JPk"),
        ("", "dd" + SECRET, "IpJrt3e7sKtzPyoXy6w-Zj6GGEvsvclN66JzQEfPYLA"),
        ("dobry-cola-super-app", SECRET, "hHz99Xs93EN1j91G9gpNepXwGNNt5YdAFkEVk_LlqdQ"),
        ("dobry-cola-super-app", "dd" + SECRET, "TGUkZaevsavLbHvlNWipnRoYxgzZ51ioWvbxgGT3wHo"),
    ],
)
def test_official_capability_vectors(path, secret, expected):
    proxy = TelegramWebProxy("PROXY.example.com" + ("/" + path if path else ""), secret)
    assert proxy.bridge_capability == expected
    assert proxy.host == "proxy.example.com"
    assert proxy.protocol == proxy.kind == "webproxy"
    for link in (proxy.tg_link(), proxy.web_link()):
        parsed = parse_line(link, "feed")
        assert parsed and parsed.key() == proxy.key()
        assert parsed.secret == secret
        assert parsed.source == "feed"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "[::1]",
        "a",
        "x.test/",
        "x.test/a//b",
        "x.test/a.b",
        "x.test/a%2Fb",
        "x.test/../x",
        "x.test:443",
        " x.test",
        "x.test/é",
        "x.test/" + "a" * 129,
    ],
)
def test_invalid_web_addresses(address):
    with pytest.raises(ValueError):
        TelegramWebProxy(address, SECRET)


def test_parser_separates_web_from_ordinary_mtproxy_and_bad_secrets():
    root = f"https://t.me/webproxy?server=proxy.example.com&secret={SECRET}"
    assert parse_line(root)
    assert parse_line(root.replace("webproxy?", "proxy?")) is None
    assert parse_line(root + "&secret=" + SECRET) is None
    assert parse_line(root + "&port=443") is None
    assert parse_line(root + "#fragment") is None
    assert parse_line(root.replace("proxy.example.com", "proxy.example.com%2FPath")) is None
    fake_tls = "ee" + SECRET + "example.com".encode().hex()
    assert parse_line(root.replace(SECRET, fake_tls)) is None
    assert parse_line(root.replace("proxy.example.com", "proxy.example.com%2G")) is None
    assert len(extract_links(root + "\n" + root + "\n" + root.replace("webproxy?", "proxy?"))) == 1


def test_base_path_marker_is_canonical_and_path_case_sensitive():
    proxy = TelegramWebProxy("proxy.example.com/Path/Sub", SECRET)
    assert "server=proxy.example.com%2FPath%2FSub" in proxy.web_link()
    parsed = parse_line(proxy.web_link())
    assert parsed and parsed.base_path == "Path/Sub"
    assert proxy.bridge_capability != TelegramWebProxy("proxy.example.com/path/sub", SECRET).bridge_capability
    assert parse_line(proxy.web_link().replace("&secret=", "&secret=%70")) is None


def test_uppercase_hex_secret_is_not_misread_as_base64_marker():
    secret = "cA" + SECRET[2:]
    proxy = TelegramWebProxy("proxy.example.com", secret)
    assert proxy.secret == secret.lower()
    assert parse_line(proxy.web_link()).secret == secret.lower()


@pytest.mark.parametrize("token", ["", "a" * 42, "a" * 44, "a" * 42 + "_", TOKEN + "=", TOKEN + "\n"])
def test_noncanonical_tokens_rejected(token):
    with pytest.raises(ValueError):
        _token(token)


def test_bridge_literals_must_bind_exact_origin_and_prefix():
    proxy = TelegramWebProxy("proxy.example.com/path", SECRET)
    assert parse_bridge(bridge(proxy), proxy) == (TOKEN, "websocket")
    for value in (
        bridge(proxy).replace(b"proxy.example.com/path/", b"evil.example.com/path/"),
        bridge(proxy).replace(b"websocket", b"unknown"),
        bridge(proxy) + bridge(proxy),
        b"const bootstrap='not-a-reference-page'",
    ):
        with pytest.raises(ValueError):
            parse_bridge(value, proxy)


@pytest.mark.parametrize(
    "batch",
    [
        b"",
        b"\x02",
        encode(DATA, 1),
        encode(WINDOW, 1, bytes(4)),
        encode(OPEN, 1),
        encode(PING, 1),
        encode(DATA, 0, b"x"),
        encode(WELCOME, 0),
        encode(DATA, 1, b"x")[:-1],
    ],
)
def test_malformed_server_frames_rejected(batch):
    with pytest.raises(ValueError):
        decode(batch)


def test_welcome_only_during_bootstrap():
    assert decode(encode(WELCOME), welcome=True)
    with pytest.raises(ValueError):
        decode(encode(WELCOME) + encode(DATA, 1, b"x"), welcome=True)
    assert (
        decode(encode(DATA, 1, b"hello") + encode(WINDOW, 1, (5).to_bytes(4, "big")))[0].payload == b"hello"
    )


def test_reference_relay_can_coalesce_64k_reads_into_larger_data_frame():
    payload = b"x" * (128 * 1024)
    assert decode(encode(DATA, 1, payload))[0].payload == payload


class Wire:
    def __init__(self):
        self.output = bytearray()
        self.closed = False

    def write(self, value):
        self.output.extend(value)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_websocket_fragmentation_and_ping_and_client_masking():
    reader = asyncio.StreamReader()
    wire = Wire()
    socket = _WebSocket(reader, wire)
    reader.feed_data(b"\x02\x02ab\x89\x01!\x80\x02cd")
    assert await socket.receive() == b"abcd"
    # Reply is a masked PONG with the exact ping bytes.
    assert wire.output[:2] == b"\x8a\x81"
    assert wire.output[-1] ^ wire.output[2] == ord("!")
    await socket.send(b"hello")
    assert wire.output[7:9] == b"\x82\x85"


@pytest.mark.parametrize(
    "wire", [b"\x81\x01x", b"\x82\x81\0\0\0\0x", b"\xc2\x01x", b"\x80\x01x", b"\x09\x01x", b"\x82\x7e\0\x01x"]
)
@pytest.mark.asyncio
async def test_websocket_bad_messages_rejected(wire):
    reader = asyncio.StreamReader()
    reader.feed_data(wire)
    with pytest.raises(ValueError):
        await _WebSocket(reader, Wire()).receive()


@pytest.mark.asyncio
async def test_websocket_upgrade_requires_accept_and_exact_subprotocol(monkeypatch):
    proxy = TelegramWebProxy("proxy.example.com/path", SECRET)
    wire = Wire()
    reader = asyncio.StreamReader()

    async def connect(*args, **kwargs):
        assert kwargs["ssl"].check_hostname
        assert kwargs["server_hostname"] == proxy.host
        return reader, wire

    monkeypatch.setattr(asyncio, "open_connection", connect)
    reader.feed_data(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
    with pytest.raises(ValueError, match="upgrade proof"):
        await _WebSocket.connect(proxy, "tproxy-v1." + TOKEN)
    assert wire.closed
    assert b"GET /path/api/v1/ws HTTP/1.1" in wire.output
    assert b"Cookie:" not in wire.output


class FixtureRelay:
    def __init__(self, proxy, mode):
        self.proxy, self.mode = proxy, mode
        self.queue = asyncio.Queue()
        self.requests = []
        self.seq, self.cursor = 0, 0
        self.data, self.windows = bytearray(), 0
        self.deleted = False
        self.bad_welcome = False
        self.bad_cursor = False

    async def http(self, request):
        self.requests.append(request)
        assert request.url.host == self.proxy.host
        assert "cookie" not in request.headers
        if request.method == "GET":
            assert str(request.url) == self.proxy.bridge_url
            return httpx.Response(
                200,
                content=bridge(self.proxy, self.mode),
                headers={"Content-Type": "text/html", "Set-Cookie": "secret=must-not-leak"},
            )
        assert request.headers["Authorization"] == "Bearer " + TOKEN
        if request.method == "DELETE":
            self.deleted = True
            return httpx.Response(204)
        if request.url.path.endswith("/session"):
            assert request.content == encode(HELLO, payload=b"\x01")
            return httpx.Response(
                200,
                content=b"wrong" if self.bad_welcome else encode(WELCOME),
                headers={"X-Session-Token": TOKEN, "X-Down-Cursor": "0", "X-Carrier-Mode": self.mode},
            )
        if self.mode.endswith("lanes"):
            assert request.headers["X-Lane-ID"] == "1"
        if request.url.path.endswith("/up"):
            assert request.headers["X-Up-Seq"] == str(self.seq + 1)
            self.seq += 1
            await self.accept(request.content)
            return httpx.Response(204, headers={"X-Up-Ack": str(self.seq)})
        assert request.headers["X-Down-Cursor"] == str(self.cursor)
        body = await self.queue.get()
        self.cursor += 1
        return httpx.Response(
            200, content=body, headers={"X-Down-Cursor": str(self.cursor + int(self.bad_cursor))}
        )

    async def accept(self, batch):
        for frame in decode(batch, server=False):
            if frame.type == DATA:
                self.data.extend(frame.payload)
                await self.queue.put(
                    encode(DATA, 1, frame.payload) + encode(WINDOW, 1, len(frame.payload).to_bytes(4, "big"))
                )
            elif frame.type == WINDOW:
                self.windows += int.from_bytes(frame.payload, "big")

    async def send(self, batch):
        await self.accept(batch)

    async def receive(self):
        return await self.queue.get()

    async def close(self):
        pass


async def fixture_carrier(monkeypatch, mode):
    proxy = TelegramWebProxy("proxy.example.com/base", SECRET)
    relay = FixtureRelay(proxy, mode)
    carrier = WebProxyCarrier(proxy)
    await carrier.client.aclose()
    carrier.client = httpx.AsyncClient(transport=httpx.MockTransport(relay.http))

    async def connect(proxy_arg, protocol):
        assert proxy_arg is proxy
        expected = "tproxy-lane-v1." + TOKEN + ".1" if mode.endswith("lanes") else "tproxy-v1." + TOKEN
        assert protocol == expected
        return relay

    monkeypatch.setattr(_WebSocket, "connect", connect)
    return carrier, relay


@pytest.mark.parametrize("mode", ["https", "https-lanes", "websocket", "websocket-lanes"])
@pytest.mark.asyncio
async def test_all_four_carriers_echo_mtproxy_bytes_and_return_consumed_window(monkeypatch, mode):
    carrier, relay = await fixture_carrier(monkeypatch, mode)
    try:
        reader, writer = await carrier.start()
        payload = bytes(range(256)) * 300
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), 2)
        assert await asyncio.wait_for(reader.readexactly(len(payload)), 2) == payload
        for _ in range(10):
            if relay.windows == len(payload):
                break
            await asyncio.sleep(0)
        assert relay.windows == len(payload)
        assert relay.data == payload
        assert carrier.credit == INITIAL_WINDOW
        assert writer.get_extra_info("telegram_webproxy_carrier") == mode
    finally:
        carrier.close()
        await carrier.wait_closed()
    assert relay.deleted


@pytest.mark.asyncio
async def test_carrier_rejects_bad_welcome_instead_of_accepting_http_success(monkeypatch):
    carrier, relay = await fixture_carrier(monkeypatch, "https")
    relay.bad_welcome = True
    try:
        with pytest.raises(ValueError):
            await carrier.start()
    finally:
        carrier.close()
        await carrier.wait_closed()


@pytest.mark.parametrize(
    "bad",
    [
        encode(WINDOW, 1, INITIAL_WINDOW.to_bytes(4, "big")),
        encode(DATA, 2, b"wrong-lane"),
        encode(PING, payload=b"not-a-lane-control"),
    ],
)
@pytest.mark.asyncio
async def test_bad_window_or_cross_lane_control_closes_carrier(monkeypatch, bad):
    carrier, relay = await fixture_carrier(monkeypatch, "websocket-lanes")
    reader, _ = await carrier.start()
    await relay.queue.put(bad)
    with pytest.raises(OSError):
        await asyncio.wait_for(reader.readexactly(1), 2)
    await carrier.wait_closed()
    assert carrier.closing


@pytest.mark.asyncio
async def test_no_receive_credit_before_bytes_are_read(monkeypatch):
    carrier, relay = await fixture_carrier(monkeypatch, "websocket")
    reader, writer = await carrier.start()
    writer.write(b"hello")
    await writer.drain()
    await asyncio.sleep(0)
    assert relay.windows == 0
    assert await reader.read(2) == b"he"
    for _ in range(10):
        if relay.windows:
            break
        await asyncio.sleep(0)
    assert relay.windows == 2
    carrier.close()
    await carrier.wait_closed()


@pytest.mark.asyncio
async def test_close_aborts_queued_bytes_and_cancels_poll(monkeypatch):
    carrier, relay = await fixture_carrier(monkeypatch, "https")
    reader, writer = await carrier.start()
    await relay.queue.put(encode(DATA, 1, b"discard") + encode(CLOSE, 1))
    assert await asyncio.wait_for(reader.read(), 2) == b""
    await writer.wait_closed()
    assert all(task.done() for task in carrier.tasks)


@pytest.mark.asyncio
async def test_reader_read_all_does_not_return_double_credit():
    class Credits:
        count = 0

        def consumed(self, value):
            self.count += value

    credits = Credits()
    reader = _Reader(credits)
    reader.feed_data(b"hello")
    reader.feed_eof()
    assert await reader.read() == b"hello"
    assert credits.count == 5


@pytest.mark.asyncio
async def test_uplink_503_retries_byte_identical_sequence(monkeypatch):
    carrier, relay = await fixture_carrier(monkeypatch, "https")
    original = relay.http
    retried = []

    async def busy_once(request):
        if request.url.path.endswith("/up") and request.content[0] == DATA:
            retried.append((request.headers["X-Up-Seq"], request.content))
            if len(retried) == 1:
                return httpx.Response(503, headers={"Retry-After": "1"})
        return await original(request)

    await carrier.client.aclose()
    carrier.client = httpx.AsyncClient(transport=httpx.MockTransport(busy_once))
    try:
        reader, writer = await carrier.start()
        writer.write(b"retry-identically")
        await asyncio.wait_for(writer.drain(), 2)
        assert await reader.readexactly(17) == b"retry-identically"
        assert len(retried) == 2 and retried[0] == retried[1]
    finally:
        carrier.close()
        await carrier.wait_closed()


@pytest.mark.asyncio
async def test_bad_downlink_cursor_fails_closed(monkeypatch):
    carrier, relay = await fixture_carrier(monkeypatch, "https")
    reader, _ = await carrier.start()
    relay.bad_cursor = True
    await relay.queue.put(encode(DATA, 1, b"not-authenticated-by-cursor"))
    with pytest.raises(OSError):
        await asyncio.wait_for(reader.readexactly(1), 2)
    await carrier.wait_closed()


@pytest.mark.asyncio
async def test_blocked_writer_can_be_cancelled_and_closed_without_task_leak(monkeypatch):
    carrier, _ = await fixture_carrier(monkeypatch, "websocket")
    _, writer = await carrier.start()
    carrier.credit = 0
    writer.write(b"blocked-until-window")
    drain = asyncio.create_task(writer.drain())
    await asyncio.sleep(0)
    assert not drain.done()
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    writer.close()
    await writer.wait_closed()
    assert all(task.done() for task in carrier.tasks)


@pytest.mark.asyncio
async def test_bootstrap_timeout_cleans_up_http_client(monkeypatch):
    instances = []

    async def hanging(self):
        instances.append(self)
        await asyncio.Event().wait()

    monkeypatch.setattr(WebProxyCarrier, "start", hanging)
    with pytest.raises(TimeoutError):
        await open_connection(TelegramWebProxy("proxy.example.com", SECRET), timeout=0.01)
    assert len(instances) == 1
    assert instances[0].client.is_closed


@pytest.mark.parametrize("amount", ["", "0", "31", "999", "-1", "tomorrow"])
def test_invalid_retry_after_rejected(amount):
    with pytest.raises(ValueError):
        WebProxyCarrier._retry_after({"Retry-After": amount})
