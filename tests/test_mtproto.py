import asyncio
import hashlib
import hmac
import os
import socket
import struct
import time

import pytest
from Crypto.Cipher import AES

from tester.faketls import CHANGE_CIPHER_SPEC, build_client_hello
from tester.mtproto import (
    ABRIDGED_TAG,
    PADDED_TAG,
    REQ_PQ_MULTI,
    RES_PQ,
    VECTOR,
    build_abridged_probe,
    build_obfuscated_handshake,
    validate_res_pq,
)
from tester.mtproto import test_proxy_once as probe_once
from tester.secrets import decode_secret
from utils.models import FailureReason, Proxy

KEY = bytes.fromhex("00112233445566778899aabbccddeeff")
TLS_SECRET = "ee" + KEY.hex() + b"www.example.com".hex()


def cipher(key, iv):
    return AES.new(key, AES.MODE_CTR, nonce=b"", initial_value=int.from_bytes(iv, "big"))


def res_pq(nonce, padded=False):
    body = struct.pack("<I", RES_PQ) + nonce + b"s" * 16
    body += b"\x08" + (0x17ED48941A08F981).to_bytes(8, "big") + bytes(3)
    body += struct.pack("<IIQ", VECTOR, 1, 0x1122334455667788)
    packet = struct.pack("<QQI", 0, (int(time.time()) << 32) | 1, len(body)) + body
    if padded:
        packet += b"pad"
    return packet


def test_handshake_uses_sha256_secret_and_explicit_dc_with_known_random(monkeypatch):
    seed = bytes(range(64))
    monkeypatch.setattr("tester.mtproto.os.urandom", lambda size: seed[:size])
    header, key, iv = build_obfuscated_handshake("dd" + KEY.hex(), dc_id=2)
    expected_key = hashlib.sha256(seed[8:40] + KEY).digest()
    assert key == expected_key
    assert iv == seed[40:56]
    decoded = cipher(expected_key, iv).decrypt(header)
    assert decoded[56:60] == PADDED_TAG
    assert decoded[60:62] == b"\x02\x00"
    assert len(header) == 64


@pytest.mark.parametrize("secret", [KEY.hex(), "dd" + KEY.hex(), TLS_SECRET])
def test_random_handshakes_differ(secret):
    assert build_obfuscated_handshake(secret)[0] != build_obfuscated_handshake(secret)[0]


def test_abridged_probe_is_real_unauthenticated_req_pq():
    nonce = b"n" * 16
    probe = build_abridged_probe(nonce)
    assert probe[0] * 4 == len(probe) - 1
    auth_key, message_id, size, constructor = struct.unpack_from("<QQII", probe, 1)
    assert auth_key == 0 and message_id % 4 == 0 and size == 20 and constructor == REQ_PQ_MULTI
    assert probe[-16:] == nonce


def test_fake_tls_hello_hmac_timestamp_and_sni():
    now = 1_800_000_000
    hello = build_client_hello(decode_secret(TLS_SECRET), timestamp=now)
    assert len(hello) == 517
    assert hello[:3] == b"\x16\x03\x01"
    assert int.from_bytes(hello[3:5], "big") == len(hello) - 5
    assert b"www.example.com" in hello
    zeroed = bytearray(hello)
    zeroed[11:43] = bytes(32)
    digest = hmac.new(KEY, zeroed, hashlib.sha256).digest()
    assert hello[11:39] == digest[:28]
    assert int.from_bytes(hello[39:43], "little") ^ int.from_bytes(digest[28:32], "little") == now


@pytest.mark.parametrize("padded", [False, True])
def test_response_validation_rejects_echo_wrong_nonce_and_malformed_vector(padded):
    nonce = b"n" * 16
    packet = res_pq(nonce, padded)
    assert validate_res_pq(packet, nonce, padded)
    assert not validate_res_pq(packet, b"x" * 16, padded)
    assert not validate_res_pq(build_abridged_probe(nonce)[1:], nonce, padded)
    bad = bytearray(packet)
    bad[20] ^= 1
    assert not validate_res_pq(bad, nonce, padded)
    assert not validate_res_pq(packet + b"x" * 16, nonce, padded)


async def read_record(reader):
    header = await reader.readexactly(5)
    return header + await reader.readexactly(int.from_bytes(header[3:5], "big"))


def server_hello(hello, wrong_key=False):
    extensions = bytes.fromhex("00330024001d0020") + os.urandom(32) + bytes.fromhex("002b00020304")
    body = b"\x03\x03" + bytes(32) + b"\x20" + hello[44:76] + b"\x13\x01\x00"
    body += struct.pack(">H", len(extensions)) + extensions
    handshake = b"\x02" + len(body).to_bytes(3, "big") + body
    response = bytearray(b"\x16\x03\x03" + struct.pack(">H", len(handshake)) + handshake)
    response += CHANGE_CIPHER_SPEC + b"\x17\x03\x03\x00\x20" + os.urandom(32)
    response[11:43] = hmac.new(
        bytes(16) if wrong_key else KEY, hello[11:43] + response, hashlib.sha256
    ).digest()
    return response


async def emulate_proxy(reader, writer, secret, behavior, errors):
    try:
        info = decode_secret(secret)
        if info.domain:
            hello = await read_record(reader)
            assert hello[:3] == b"\x16\x03\x01"
            zeroed = bytearray(hello)
            zeroed[11:43] = bytes(32)
            assert hello[11:39] == hmac.new(KEY, zeroed, hashlib.sha256).digest()[:28]
            writer.write(server_hello(hello, wrong_key=behavior == "wrong_tls_hash"))
            await writer.drain()
            if behavior == "wrong_tls_hash":
                return
            assert await reader.readexactly(6) == CHANGE_CIPHER_SPEC
            wrapped = await read_record(reader)
            assert wrapped[:3] == b"\x17\x03\x03"
            header, encrypted_probe = wrapped[5:69], wrapped[69:]
        else:
            header = await reader.readexactly(64)
            encrypted_probe = None
        decryptor = cipher(hashlib.sha256(header[8:40] + KEY).digest(), header[40:56])
        decoded_header = decryptor.decrypt(header)
        assert decoded_header[56:60] == (PADDED_TAG if info.padded else ABRIDGED_TAG)
        assert decoded_header[60:62] == b"\x02\x00"
        if encrypted_probe is not None:
            frame = decryptor.decrypt(encrypted_probe)
            size = int.from_bytes(frame[:4], "little")
            packet = frame[4 : 4 + size]
        elif info.padded:
            size = int.from_bytes(decryptor.decrypt(await reader.readexactly(4)), "little")
            packet = decryptor.decrypt(await reader.readexactly(size))
        else:
            size = decryptor.decrypt(await reader.readexactly(1))[0] * 4
            packet = decryptor.decrypt(await reader.readexactly(size))
        assert packet[:8] == bytes(8)
        assert struct.unpack_from("<I", packet, 16)[0] == 20
        assert struct.unpack_from("<I", packet, 20)[0] == REQ_PQ_MULTI
        nonce = packet[24:40]
        response = res_pq(b"x" * 16 if behavior == "wrong_nonce" else nonce, info.padded)
        reversed_seed = header[55:7:-1]
        encryptor = cipher(hashlib.sha256(reversed_seed[:32] + KEY).digest(), reversed_seed[32:])
        frame = (
            struct.pack("<I", len(response)) + response
            if info.padded
            else bytes([len(response) // 4]) + response
        )
        encrypted = encryptor.encrypt(frame)
        if behavior == "oversize":
            encrypted = cipher(hashlib.sha256(reversed_seed[:32] + KEY).digest(), reversed_seed[32:]).encrypt(
                struct.pack("<I", 100_000) if info.padded else b"\x7f\xff\xff\xff"
            )
        if info.domain:
            # Split the encrypted stream across TLS records to test record reassembly.
            for fragment in (encrypted[:2], encrypted[2:]):
                writer.write(b"\x17\x03\x03" + struct.pack(">H", len(fragment)) + fragment)
                await writer.drain()
        else:
            for index in range(0, len(encrypted), 7):
                writer.write(encrypted[index : index + 7])
                await writer.drain()
                await asyncio.sleep(0)
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    except Exception as exc:
        errors.append(exc)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
@pytest.mark.parametrize("secret", [KEY.hex(), "dd" + KEY.hex(), TLS_SECRET])
@pytest.mark.parametrize("behavior,success", [("valid", True), ("wrong_nonce", False), ("oversize", False)])
async def test_real_loopback_protocol_exchange(secret, behavior, success):
    errors = []
    tasks = []

    def handle(reader, writer):
        tasks.append(asyncio.create_task(emulate_proxy(reader, writer, secret, behavior, errors)))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        sample = await probe_once(Proxy("127.0.0.1", server.sockets[0].getsockname()[1], secret), 1, 1)
    await asyncio.gather(*tasks)
    assert not errors
    assert sample.success is success
    assert sample.failure_reason == (FailureReason.NONE if success else FailureReason.HANDSHAKE_INVALID)


@pytest.mark.asyncio
async def test_fake_tls_server_hash_is_required():
    errors = []
    tasks = []

    def handle(reader, writer):
        tasks.append(asyncio.create_task(emulate_proxy(reader, writer, TLS_SECRET, "wrong_tls_hash", errors)))

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        sample = await probe_once(Proxy("127.0.0.1", server.sockets[0].getsockname()[1], TLS_SECRET), 1, 1)
    await asyncio.gather(*tasks)
    assert not errors
    assert sample.failure_reason == FailureReason.HANDSHAKE_INVALID


@pytest.mark.asyncio
async def test_plain_http_banner_is_not_healthy():
    async def handle(reader, writer):
        await reader.read(512)
        writer.write(b"HTTP/1.1 200 OK\r\n\r\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        sample = await probe_once(Proxy("127.0.0.1", server.sockets[0].getsockname()[1], KEY.hex()), 1, 1)
    assert not sample.success
    assert sample.failure_reason == FailureReason.HANDSHAKE_INVALID


@pytest.mark.asyncio
async def test_invalid_secret_fails_before_any_connection(monkeypatch):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Malformed secret must not open a socket")

    monkeypatch.setattr("tester.mtproto.asyncio.open_connection", forbidden)
    sample = await probe_once(Proxy("localhost", 443, "ee" + KEY.hex()))
    assert sample.failure_reason == FailureReason.SECRET_INVALID


@pytest.mark.asyncio
async def test_dns_errors_remain_distinguishable(monkeypatch):
    async def fail(*args, **kwargs):
        raise socket.gaierror("No such host")

    monkeypatch.setattr("tester.mtproto.asyncio.open_connection", fail)
    sample = await probe_once(Proxy("bad.example", 443, KEY.hex()))
    assert sample.failure_reason == FailureReason.DNS_ERROR


@pytest.mark.asyncio
async def test_handshake_timeout_and_cancellation_close_the_socket(monkeypatch):
    class Writer:
        closed = False

        def close(self):
            self.closed = True

        async def wait_closed(self):
            return None

    writer = Writer()

    async def connect(*args, **kwargs):
        return object(), writer

    entered = asyncio.Event()

    async def stalled(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr("tester.mtproto.asyncio.open_connection", connect)
    monkeypatch.setattr("tester.mtproto._exchange_probe", stalled)
    sample = await probe_once(Proxy("localhost", 443, KEY.hex()), 1, 0.01)
    assert sample.failure_reason == FailureReason.HANDSHAKE_TIMEOUT and writer.closed
    writer.closed = False
    task = asyncio.create_task(probe_once(Proxy("localhost", 443, KEY.hex())))
    await entered.wait()
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.closed
