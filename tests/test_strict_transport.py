"""The session verifier must travel through the selected MTProxy transport."""

import asyncio
import hashlib
import struct
import sys
from types import SimpleNamespace

import pytest

from tester import mtproto
from tester.faketls import CHANGE_CIPHER_SPEC
from tester.secrets import decode_secret
from tests.test_mtproto import KEY, TLS_SECRET, cipher, emulate_proxy, read_record, server_hello
from utils.models import Proxy

PLAIN = struct.pack("<QQI", 0, 4, 20) + b"request-body".ljust(20, b".")
ENCRYPTED = b"auth-key" + b"message-key".ljust(16, b".") + b"c" * 512
REPLY = struct.pack("<QQI", 0, 5, 20) + b"reply-body".ljust(20, b".")


def strip_padding(packet, padded):
    if not padded:
        return packet
    if packet[:8] == bytes(8):
        return packet[: 20 + int.from_bytes(packet[16:20], "little")]
    padding = (len(packet) - 24) % 16
    return packet[:-padding] if padding else packet


async def multi_packet_proxy(reader, writer, secret, errors, dc_ids, reply_padding=3):
    info = decode_secret(secret)
    try:
        initial = b""
        if info.domain:
            hello = await read_record(reader)
            writer.write(server_hello(hello))
            await writer.drain()
            assert await reader.readexactly(6) == CHANGE_CIPHER_SPEC
            first = (await read_record(reader))[5:]
            header, initial = first[:64], first[64:]
        else:
            header = await reader.readexactly(64)
        decryptor = cipher(hashlib.sha256(header[8:40] + KEY).digest(), header[40:56])
        decoded = decryptor.decrypt(header)
        dc_ids.append(struct.unpack_from("<h", decoded, 60)[0])
        reversed_seed = header[55:7:-1]
        encryptor = cipher(hashlib.sha256(reversed_seed[:32] + KEY).digest(), reversed_seed[32:])
        buffer = bytearray(decryptor.decrypt(initial))

        async def readn(size):
            while len(buffer) < size:
                chunk = (
                    (await read_record(reader))[5:]
                    if info.domain
                    else await reader.readexactly(size - len(buffer))
                )
                buffer.extend(decryptor.decrypt(chunk))
            result = bytes(buffer[:size])
            del buffer[:size]
            return result

        for expected in (PLAIN, ENCRYPTED, PLAIN):
            if info.padded:
                size = int.from_bytes(await readn(4), "little")
            else:
                prefix = (await readn(1))[0]
                size = (int.from_bytes(await readn(3), "little") if prefix == 127 else prefix) * 4
            assert strip_padding(await readn(size), info.padded) == expected
            reply = REPLY + (b"p" * reply_padding if info.padded else b"")
            framed = (
                struct.pack("<I", len(reply)) + reply if info.padded else bytes([len(reply) // 4]) + reply
            )
            encrypted = encryptor.encrypt(framed)
            for chunk in (encrypted[:2], encrypted[2:]):
                writer.write(
                    b"\x17\x03\x03" + struct.pack(">H", len(chunk)) + chunk if info.domain else chunk
                )
                await writer.drain()
        # A completed probe must close its socket after protocol cleanup.
        assert await asyncio.wait_for(reader.read(1), 2) == b""
    except Exception as exc:
        errors.append(exc)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [KEY.hex(), "dd" + KEY.hex(), TLS_SECRET])
async def test_nonce_reply_alone_does_not_pass_strict_verification(secret):
    """A valid-shaped resPQ with invented RSA fingerprints is not Telegram proof."""
    tasks, errors = [], []

    def handle(reader, writer):
        tasks.append(asyncio.create_task(emulate_proxy(reader, writer, secret, "valid", errors)))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        # Fall back solely to reproduce the old permissive behavior before this API exists.
        probe = getattr(mtproto, "test_proxy_strict", mtproto.test_proxy_once)
        sample = await probe(Proxy("127.0.0.1", server.sockets[0].getsockname()[1], secret), 1, 1)
    await asyncio.gather(*tasks)
    assert not errors
    assert not sample.success, "Unauthenticated resPQ was mistaken for an encrypted Telegram session"


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [KEY.hex(), "dd" + KEY.hex(), TLS_SECRET])
@pytest.mark.parametrize("reply_padding", [3, 71, 115, 255])
async def test_strict_session_uses_all_frames_same_stream_and_requested_dc(
    secret, monkeypatch, reply_padding
):
    errors, tasks, dc_ids = [], [], []

    async def verify(send, receive, dc_id, **kwargs):
        assert dc_id == 4
        for packet in (PLAIN, ENCRYPTED, PLAIN):
            await send(packet)
            assert await receive() == REPLY
        return SimpleNamespace(authenticated=True, ping_rtts_ms=[4.0, 6.0, 5.0], handshake_ms=2.0)

    monkeypatch.setitem(sys.modules, "tester.telegram_session", SimpleNamespace(verify_session=verify))

    def handle(reader, writer):
        tasks.append(
            asyncio.create_task(multi_packet_proxy(reader, writer, secret, errors, dc_ids, reply_padding))
        )

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        sample = await mtproto.test_proxy_strict(
            Proxy("127.0.0.1", server.sockets[0].getsockname()[1], secret), 1, 1, dc_id=4
        )
    await asyncio.gather(*tasks)
    assert not errors
    assert dc_ids == [4]
    assert sample.success and sample.verification_level == "telegram_session"
    assert sample.dc_id == 4 and sample.ping_rtts_ms == [4.0, 6.0, 5.0]
    assert sample.total_latency_ms >= 0 and sample.handshake_latency_ms == 2.0


@pytest.mark.asyncio
async def test_strict_cancellation_closes_socket_and_remains_cancellation(monkeypatch):
    entered, closed = asyncio.Event(), asyncio.Event()

    async def verify(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def handle(reader, writer):
        await reader.read()
        writer.close()
        await writer.wait_closed()
        closed.set()

    monkeypatch.setitem(sys.modules, "tester.telegram_session", SimpleNamespace(verify_session=verify))
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        task = asyncio.create_task(
            mtproto.test_proxy_strict(Proxy("127.0.0.1", server.sockets[0].getsockname()[1], KEY.hex()), 1, 1)
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(closed.wait(), 2)
