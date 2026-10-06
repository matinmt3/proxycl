"""Validate MTProxy transport by requiring a nonce-matched MTProto resPQ.

Supports raw, dd padded, and ee FakeTLS secrets. This unauthenticated probe
checks the proxy's ability to answer MTProto; it does not log in, create an
authorization key, or authenticate the Telegram server's RSA identity.
Protocol references: core.telegram.org/mtproto/mtproto-transports and TDLib's
mtproto_api.tl, ProxySecret.cpp, TlsInit.cpp, and TcpTransport.cpp.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import struct
import time

from Crypto.Cipher import AES
from Crypto.Util import Counter

from tester.faketls import authenticate_server_hello, build_client_hello, read_tls_record, wrap_tls_payload
from tester.secrets import ProxySecret, decode_secret, normalize_port, normalize_server
from utils.models import FailureReason, Proxy, TestSample

ABRIDGED_TAG = b"\xef" * 4
PADDED_TAG = b"\xdd" * 4
REQ_PQ_MULTI = 0xBE7E8EF1
RES_PQ = 0x05162463
VECTOR = 0x1CB5C415
MAX_PROBE_RESPONSE = 4096
_FORBIDDEN_FIRST_WORDS = {b"HEAD", b"POST", b"GET ", b"OPTI", b"\x16\x03\x01\x02", PADDED_TAG, b"\xee" * 4}


def _make_aes_ctr(key: bytes, iv: bytes):
    ctr = Counter.new(128, initial_value=int.from_bytes(iv, "big"))
    return AES.new(key, AES.MODE_CTR, counter=ctr)


def _build_transport(secret: ProxySecret, dc_id: int = 2):
    for _ in range(100):
        raw = bytearray(os.urandom(64))
        if raw[0] != 0xEF and bytes(raw[:4]) not in _FORBIDDEN_FIRST_WORDS and raw[4:8] != bytes(4):
            break
    else:
        raise ValueError("Could not generate an obfuscated header")
    raw[56:60] = PADDED_TAG if secret.padded else ABRIDGED_TAG
    raw[60:62] = struct.pack("<h", dc_id)
    key = hashlib.sha256(bytes(raw[8:40]) + secret.key).digest()
    iv = bytes(raw[40:56])
    reversed_header = bytes(raw[::-1])
    decrypt_key = hashlib.sha256(reversed_header[8:40] + secret.key).digest()
    decrypt_iv = reversed_header[40:56]
    encryptor = _make_aes_ctr(key, iv)
    encrypted = encryptor.encrypt(raw)
    header = bytes(raw[:56]) + encrypted[56:64]
    return header, encryptor, _make_aes_ctr(decrypt_key, decrypt_iv), key, iv


def build_obfuscated_handshake(secret_hex: str, dc_id: int = 2) -> tuple[bytes, bytes, bytes]:
    """Compatibility helper returning the header and outgoing key/IV.

    The caller must advance its AES stream by 64 bytes before encrypting data.
    Runtime exchange uses _build_transport to retain that stream automatically.
    """
    header, _, _, key, iv = _build_transport(decode_secret(secret_hex), dc_id)
    return header, key, iv


def _unencrypted_message(body: bytes) -> bytes:
    message_id = (time.time_ns() * (1 << 32) // 1_000_000_000) & ~3
    return struct.pack("<QQI", 0, message_id, len(body)) + body


def build_abridged_probe(nonce: bytes | None = None) -> bytes:
    """Build an unauthenticated req_pq_multi envelope with a fresh 128-bit nonce."""
    nonce = os.urandom(16) if nonce is None else nonce
    if len(nonce) != 16:
        raise ValueError("Probe nonce must be 16 bytes")
    packet = _unencrypted_message(struct.pack("<I", REQ_PQ_MULTI) + nonce)
    return bytes([len(packet) // 4]) + packet


def _build_probe(nonce: bytes, padded: bool) -> bytes:
    abridged = build_abridged_probe(nonce)
    if not padded:
        return abridged
    padding = os.urandom(os.urandom(1)[0] % 16)
    packet = abridged[1:] + padding
    return struct.pack("<I", len(packet)) + packet


class _EncryptedReader:
    def __init__(self, reader, decryptor, fake_tls: bool):
        self.reader = reader
        self.decryptor = decryptor
        self.fake_tls = fake_tls
        self.buffer = bytearray()

    async def readexactly(self, count: int) -> bytes:
        if not self.fake_tls:
            return self.decryptor.decrypt(await self.reader.readexactly(count))
        while len(self.buffer) < count:
            record = await read_tls_record(self.reader, 23)
            self.buffer.extend(self.decryptor.decrypt(record[5:]))
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result


async def _read_frame(reader: _EncryptedReader, padded: bool) -> bytes:
    if padded:
        size = int.from_bytes(await reader.readexactly(4), "little")
    else:
        prefix = (await reader.readexactly(1))[0]
        if prefix == 0x7F:
            words = int.from_bytes(await reader.readexactly(3), "little")
            if words < 127:
                raise ValueError("Noncanonical abridged length")
            size = words * 4
        elif 0 < prefix < 0x7F:
            size = prefix * 4
        else:
            raise ValueError("Invalid abridged length")
    if not 20 <= size <= MAX_PROBE_RESPONSE:
        raise ValueError("Invalid MTProto response length")
    return await reader.readexactly(size)


def validate_res_pq(packet: bytes, nonce: bytes, padded: bool = False) -> bool:
    """Reject arbitrary banners, echo servers, malformed TL, and mismatched nonces."""
    if len(nonce) != 16 or len(packet) < 20:
        return False
    auth_key_id, message_id, body_size = struct.unpack_from("<QQI", packet)
    padding_size = len(packet) - 20 - body_size
    if auth_key_id != 0 or message_id % 2 != 1 or body_size % 4 or body_size < 56:
        return False
    if padding_size < 0 or padding_size > (15 if padded else 0):
        return False
    body = packet[20 : 20 + body_size]
    if struct.unpack_from("<I", body)[0] != RES_PQ or body[4:20] != nonce:
        return False
    # resPQ contains server_nonce:int128, pq:string, Vector<long> fingerprints.
    offset = 36
    pq_size = body[offset]
    if not 1 <= pq_size <= 8:
        return False
    offset += 1 + pq_size
    offset += (-offset) % 4
    if len(body) < offset + 8:
        return False
    constructor, count = struct.unpack_from("<II", body, offset)
    return constructor == VECTOR and 1 <= count <= 64 and offset + 8 + count * 8 == len(body)


async def _exchange_probe(reader, writer, secret: ProxySecret) -> None:
    if secret.domain:
        hello = build_client_hello(secret)
        writer.write(hello)
        await writer.drain()
        await authenticate_server_hello(reader, secret, hello)
    header, encryptor, decryptor, _, _ = _build_transport(secret)
    nonce = os.urandom(16)
    payload = header + encryptor.encrypt(_build_probe(nonce, secret.padded))
    writer.write(wrap_tls_payload(payload, first=True) if secret.domain else payload)
    await writer.drain()
    packet = await _read_frame(_EncryptedReader(reader, decryptor, bool(secret.domain)), secret.padded)
    if not validate_res_pq(packet, nonce, secret.padded):
        raise ValueError("Proxy did not return a valid nonce-matched MTProto resPQ")


async def test_proxy_once(
    proxy: Proxy, connect_timeout: float = 3.0, handshake_timeout: float = 5.0
) -> TestSample:
    start = time.perf_counter()
    writer = None
    tcp_latency_ms = None

    def failed(reason: FailureReason) -> TestSample:
        return TestSample(
            success=False,
            failure_reason=reason,
            tcp_latency_ms=tcp_latency_ms,
            total_latency_ms=(time.perf_counter() - start) * 1000,
        )

    try:
        secret = decode_secret(proxy.secret)
    except ValueError:
        return failed(FailureReason.SECRET_INVALID)
    try:
        server = normalize_server(proxy.server)
        port = normalize_port(proxy.port)
    except ValueError:
        return failed(FailureReason.DNS_ERROR)
    try:
        connect_start = time.perf_counter()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(server, port), timeout=connect_timeout
        )
        tcp_latency_ms = (time.perf_counter() - connect_start) * 1000
    except socket.gaierror:
        return failed(FailureReason.DNS_ERROR)
    except asyncio.TimeoutError:
        return failed(FailureReason.TCP_TIMEOUT)
    except OSError:
        return failed(FailureReason.TCP_REFUSED)
    try:
        handshake_start = time.perf_counter()
        await asyncio.wait_for(_exchange_probe(reader, writer, secret), timeout=handshake_timeout)
        return TestSample(
            success=True,
            tcp_latency_ms=tcp_latency_ms,
            handshake_latency_ms=(time.perf_counter() - handshake_start) * 1000,
            total_latency_ms=(time.perf_counter() - start) * 1000,
        )
    except asyncio.TimeoutError:
        return failed(FailureReason.HANDSHAKE_TIMEOUT)
    except (ValueError, asyncio.IncompleteReadError, OSError):
        return failed(FailureReason.HANDSHAKE_INVALID)
    except Exception:
        return failed(FailureReason.UNKNOWN)
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=0.5)
            except (OSError, asyncio.TimeoutError):
                pass
