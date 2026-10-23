"""WEB carrier handoff into the actual inner MTProxy probes.

These controlled loopback peers are not certified public WEB relays. The
multi-packet verifier stub isolates transport continuity; RSA/DH identity and
encrypted session proof have separate strict-crypto tests.
"""

import asyncio
import sys
from types import SimpleNamespace

import pytest

from telegram_webproxy import TelegramWebProxy
from telegram_webproxy import carrier as web_carrier
from tester import mtproto
from tests.test_mtproto import KEY, emulate_proxy
from tests.test_strict_transport import ENCRYPTED, PLAIN, REPLY, multi_packet_proxy
from utils.models import FailureReason, ProxyResult


def map_web_carrier_to_local_stream(monkeypatch, port):
    """Fail any direct scanner connection; only the explicit carrier may dial."""
    original_open = asyncio.open_connection
    calls, direct, writers = [], [], []

    async def mapped(proxy, *, timeout):
        calls.append((proxy, timeout))
        reader, writer = await original_open("127.0.0.1", port)
        writers.append(writer)
        return reader, writer

    async def forbidden(*args, **kwargs):
        direct.append((args, kwargs))
        raise AssertionError("WEB scan attempted direct endpoint TCP fallback")

    monkeypatch.setattr(web_carrier, "open_connection", mapped)
    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    return calls, direct, writers


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [KEY.hex(), "dd" + KEY.hex()])
async def test_web_nonce_only_screening_pass_is_never_strict_session_proof(secret, monkeypatch):
    errors, tasks = [], []

    def handle(reader, writer):
        tasks.append(asyncio.create_task(emulate_proxy(reader, writer, secret, "valid", errors)))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    proxy = TelegramWebProxy("relay.example.com/TestPath", secret)
    calls, direct, writers = map_web_carrier_to_local_stream(monkeypatch, server.sockets[0].getsockname()[1])
    async with server:
        screened = await mtproto.test_proxy_once(proxy, 1, 1)
        strict = await mtproto.test_proxy_strict(proxy, 1, 1)
    await asyncio.gather(*tasks)
    assert not errors and not direct
    assert calls == [(proxy, 2), (proxy, 2)]
    assert all(writer.is_closing() for writer in writers)
    assert screened.success and screened.verification_level == "transport"
    assert not ProxyResult(proxy, [screened] * 3).strict_eligible
    assert not strict.success and strict.failure_reason == FailureReason.HANDSHAKE_INVALID
    assert strict.verification_level == "telegram_session" and strict.ping_rtts_ms == []
    assert not ProxyResult(proxy, [strict] * 3).strict_eligible


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [KEY.hex(), "dd" + KEY.hex()])
@pytest.mark.parametrize("reply_padding", [3, 255])
async def test_web_strict_transport_preserves_requested_dc_and_one_obfuscated_stream(
    secret, reply_padding, monkeypatch
):
    errors, tasks, dc_ids, received = [], [], [], []

    async def verify(send, receive, dc_id, **kwargs):
        assert dc_id == 4
        assert kwargs["ping_count"] == 3
        for packet in (PLAIN, ENCRYPTED, PLAIN):
            await send(packet)
            received.append(await receive())
        return SimpleNamespace(authenticated=True, ping_rtts_ms=[4.0, 6.0, 5.0], handshake_ms=2.0)

    monkeypatch.setitem(sys.modules, "tester.telegram_session", SimpleNamespace(verify_session=verify))

    def handle(reader, writer):
        tasks.append(
            asyncio.create_task(multi_packet_proxy(reader, writer, secret, errors, dc_ids, reply_padding))
        )

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    proxy = TelegramWebProxy("relay.example.com/TestPath", secret)
    calls, direct, writers = map_web_carrier_to_local_stream(monkeypatch, server.sockets[0].getsockname()[1])
    async with server:
        sample = await mtproto.test_proxy_strict(proxy, 1, 1, dc_id=4, ping_count=3)
    await asyncio.gather(*tasks)
    assert not errors and not direct
    assert calls == [(proxy, 2)] and len(writers) == 1 and writers[0].is_closing()
    assert dc_ids == [4] and received == [REPLY] * 3
    assert sample.success and sample.verification_level == "telegram_session"
    assert sample.dc_id == 4 and sample.ping_rtts_ms == [4.0, 6.0, 5.0]


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_name", ["test_proxy_once", "test_proxy_strict"])
@pytest.mark.parametrize(
    "exception,reason",
    [
        (OSError, FailureReason.TCP_REFUSED),
        (TimeoutError, FailureReason.TCP_TIMEOUT),
        (ValueError, FailureReason.HANDSHAKE_INVALID),
    ],
)
async def test_failed_web_carrier_never_falls_back_to_direct_endpoint_tcp(
    probe_name, exception, reason, monkeypatch
):
    calls, direct = [], []
    proxy = TelegramWebProxy("relay.example.com", KEY.hex())

    async def failed(candidate, *, timeout):
        calls.append((candidate, timeout))
        raise exception("Controlled unavailable WEB carrier")

    async def forbidden(*args, **kwargs):
        direct.append((args, kwargs))
        raise AssertionError("WEB carrier failure triggered a direct endpoint connection")

    monkeypatch.setattr(web_carrier, "open_connection", failed)
    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    sample = await getattr(mtproto, probe_name)(proxy, 1, 2)
    assert calls == [(proxy, 3)] and not direct
    assert not sample.success and sample.failure_reason == reason
    assert sample.ping_rtts_ms == []


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_name", ["test_proxy_once", "test_proxy_strict"])
async def test_cancelled_web_probe_closes_handed_off_carrier_stream_and_remains_cancelled(
    probe_name, monkeypatch
):
    entered, closed = asyncio.Event(), asyncio.Event()
    tasks = []

    async def stalled(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def peer(reader, writer):
        try:
            assert await asyncio.wait_for(reader.read(), 2) == b""
        finally:
            writer.close()
            await writer.wait_closed()
            closed.set()

    def handle(reader, writer):
        tasks.append(asyncio.create_task(peer(reader, writer)))

    if probe_name == "test_proxy_once":
        monkeypatch.setattr(mtproto, "_exchange_probe", stalled)
    else:
        monkeypatch.setitem(sys.modules, "tester.telegram_session", SimpleNamespace(verify_session=stalled))
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    proxy = TelegramWebProxy("relay.example.com/TestPath", KEY.hex())
    calls, direct, writers = map_web_carrier_to_local_stream(monkeypatch, server.sockets[0].getsockname()[1])
    async with server:
        task = asyncio.create_task(getattr(mtproto, probe_name)(proxy, 1, 1))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 2)
    await asyncio.gather(*tasks)
    assert calls == [(proxy, 2)] and not direct
    assert len(writers) == 1 and writers[0].is_closing()
