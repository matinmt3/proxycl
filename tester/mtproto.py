"""MTProto-level proxy validation.

Unlike a plain TCP ping, this module speaks the actual MTProto client-to-proxy
"obfuscated2" handshake that real Telegram clients use when connecting through
an MTProto proxy (this handshake, and the abridged framing format, are
documented publicly as part of Telegram's own open-source MTProxy code).

For each proxy we:
  1. Open a raw TCP connection and time it.
  2. Build the 64-byte obfuscated2 handshake, keyed with the proxy's secret,
     and send it.
  3. Send a minimal abridged-framed probe packet.
  4. Wait for the proxy to respond with *something* (bytes, or at minimum
     keep the socket open without an immediate RST/FIN) within the timeout.

A proxy that isn't a real, live MTProto endpoint (dead IP, wrong secret,
plain HTTP server, firewall drop, etc.) will fail at one of these steps,
which is a much stronger signal than "the TCP port is open".

Full application-layer validation (completing a DH key exchange with the
Telegram DC behind the proxy) is out of scope for a lightweight health
checker and is noted as a possible extension in the README.
"""
from __future__ import annotations

import asyncio
import os
import struct
import time
from typing import Optional

from Crypto.Cipher import AES
from Crypto.Util import Counter

from utils.models import FailureReason, Proxy, TestSample

# Bytes that must not appear as the first byte of the random handshake header,
# since they collide with other well-known protocols (TLS, HTTP, etc.)
_FORBIDDEN_FIRST_BYTES = {0xEF, 0x16, 0x50, 0x47, 0x48, 0xDD}
_FORBIDDEN_FIRST_WORDS = {b"HEAD", b"POST", b"GET ", b"OPTI", b"\x00\x00\x00\x00"}

ABRIDGED_TAG = b"\xef\xef\xef\xef"


def _make_aes_ctr(key: bytes, iv: bytes):
    ctr = Counter.new(128, initial_value=int.from_bytes(iv, "big"))
    return AES.new(key, AES.MODE_CTR, counter=ctr)


def build_obfuscated_handshake(secret_hex: str) -> tuple[bytes, bytes, bytes]:
    """Build the 64-byte obfuscated2 handshake header.

    Returns (header_bytes, encrypt_key, encrypt_iv) so the caller can keep
    encrypting subsequent bytes on the same stream.
    """
    secret = bytes.fromhex(secret_hex[-32:]) if len(secret_hex) >= 32 else bytes.fromhex(secret_hex.zfill(32))

    while True:
        random_bytes = bytearray(os.urandom(64))
        if random_bytes[0] in _FORBIDDEN_FIRST_BYTES:
            continue
        if bytes(random_bytes[0:4]) in _FORBIDDEN_FIRST_WORDS:
            continue
        if random_bytes[4:8] == b"\x00\x00\x00\x00":
            continue
        break

    # Key/IV material for client -> proxy direction, taken from bytes 8..56
    key = bytes(random_bytes[8:40])
    iv = bytes(random_bytes[40:56])

    # Mix in the shared secret (simple secrets: XOR key with secret bytes)
    if secret:
        mixed = bytearray(key)
        for i in range(len(mixed)):
            mixed[i] ^= secret[i % len(secret)]
        key = bytes(mixed)

    # Reversed copy is used for the proxy -> client (decryption) direction.
    reversed_random = bytes(random_bytes[55:7:-1])
    dec_key = reversed_random[0:32]
    if secret:
        mixed = bytearray(dec_key)
        for i in range(len(mixed)):
            mixed[i] ^= secret[i % len(secret)]
        dec_key = bytes(mixed)

    # Protocol tag for "abridged" framing goes in bytes 56:60
    random_bytes[56:60] = ABRIDGED_TAG

    # Encrypt the full 64-byte buffer with itself as keystream source, then
    # splice back bytes 56:64 in plaintext-of-the-encrypted form (this is
    # the standard obfuscated2 trick so the server can derive the same key
    # purely from what's on the wire).
    encryptor = _make_aes_ctr(key, iv)
    encrypted = bytearray(encryptor.encrypt(bytes(random_bytes)))
    header = bytes(random_bytes[0:56]) + bytes(encrypted[56:64])

    return header, key, iv


def build_abridged_probe() -> bytes:
    """A minimal abridged-framed packet used purely as a liveness probe.

    Abridged framing: 1 length byte (in 4-byte words) followed by payload.
    We send a tiny, well-formed but inert padding packet so a real MTProto
    endpoint has something syntactically valid to (not) act on, without
    requiring a full authorization key exchange.
    """
    payload = struct.pack("<I", 0) + os.urandom(8)  # 12 bytes, benign padding
    length_words = len(payload) // 4
    if length_words < 0x7F:
        return bytes([length_words]) + payload
    return b"\x7f" + length_words.to_bytes(3, "little") + payload


async def test_proxy_once(
    proxy: Proxy,
    connect_timeout: float = 3.0,
    handshake_timeout: float = 5.0,
) -> TestSample:
    """Run one full connect + MTProto handshake attempt against a proxy."""
    start = time.perf_counter()
    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None

    try:
        connect_start = time.perf_counter()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(proxy.server, proxy.port),
            timeout=connect_timeout,
        )
        tcp_latency_ms = (time.perf_counter() - connect_start) * 1000
    except asyncio.TimeoutError:
        return TestSample(
            success=False,
            failure_reason=FailureReason.TCP_TIMEOUT,
            total_latency_ms=(time.perf_counter() - start) * 1000,
        )
    except (ConnectionRefusedError, OSError):
        return TestSample(
            success=False,
            failure_reason=FailureReason.TCP_REFUSED,
            total_latency_ms=(time.perf_counter() - start) * 1000,
        )

    try:
        handshake_start = time.perf_counter()
        try:
            header, key, iv = build_obfuscated_handshake(proxy.secret)
        except ValueError:
            return TestSample(
                success=False,
                failure_reason=FailureReason.SECRET_INVALID,
                tcp_latency_ms=tcp_latency_ms,
                total_latency_ms=(time.perf_counter() - start) * 1000,
            )

        writer.write(header)
        await writer.drain()

        encryptor = _make_aes_ctr(key, iv)
        probe = build_abridged_probe()
        writer.write(encryptor.encrypt(probe))
        await writer.drain()

        try:
            data = await asyncio.wait_for(reader.read(64), timeout=handshake_timeout)
        except asyncio.TimeoutError:
            return TestSample(
                success=False,
                failure_reason=FailureReason.HANDSHAKE_TIMEOUT,
                tcp_latency_ms=tcp_latency_ms,
                total_latency_ms=(time.perf_counter() - start) * 1000,
            )

        handshake_latency_ms = (time.perf_counter() - handshake_start) * 1000
        total_latency_ms = (time.perf_counter() - start) * 1000

        # An immediate clean close (b"") right after our handshake strongly
        # suggests the remote isn't a live MTProto endpoint (dead proxy,
        # wrong secret rejected, or a non-MTProto service on that port).
        if data == b"":
            return TestSample(
                success=False,
                failure_reason=FailureReason.HANDSHAKE_INVALID,
                tcp_latency_ms=tcp_latency_ms,
                total_latency_ms=total_latency_ms,
            )

        return TestSample(
            success=True,
            tcp_latency_ms=tcp_latency_ms,
            handshake_latency_ms=handshake_latency_ms,
            total_latency_ms=total_latency_ms,
        )
    except (ConnectionResetError, OSError):
        return TestSample(
            success=False,
            failure_reason=FailureReason.HANDSHAKE_INVALID,
            tcp_latency_ms=tcp_latency_ms,
            total_latency_ms=(time.perf_counter() - start) * 1000,
        )
    except Exception:  # noqa: BLE001
        return TestSample(
            success=False,
            failure_reason=FailureReason.UNKNOWN,
            tcp_latency_ms=tcp_latency_ms,
            total_latency_ms=(time.perf_counter() - start) * 1000,
        )
    finally:
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
