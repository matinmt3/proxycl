"""MTProxy FakeTLS camouflage, compatible with Telegram's TDLib transport.

This is the proxy protocol's authenticated TLS-shaped record wrapper; it does
not perform a TLS key exchange or grant access to a Telegram account.
See TDLib td/mtproto/TlsInit.cpp and TcpTransport.cpp.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import struct
import time

from Crypto.PublicKey import ECC

from tester.secrets import ProxySecret

TLS_MAX_RECORD = 16384
CHANGE_CIPHER_SPEC = b"\x14\x03\x03\x00\x01\x01"


def _extension(kind: int, body: bytes) -> bytes:
    return struct.pack(">HH", kind, len(body)) + body


def build_client_hello(secret: ProxySecret, timestamp: int | None = None) -> bytes:
    if not secret.domain:
        raise ValueError("FakeTLS requires a domain")
    domain = secret.domain.encode("ascii")
    session_id = os.urandom(32)
    curve_key = ECC.generate(curve="Curve25519").public_key().export_key(format="raw")
    sni_entry = b"\x00" + struct.pack(">H", len(domain)) + domain
    protocols = b"\x02h2\x08http/1.1"
    extensions = b"".join(
        [
            _extension(0, struct.pack(">H", len(sni_entry)) + sni_entry),
            _extension(10, b"\x00\x06\x00\x1d\x00\x17\x00\x18"),
            _extension(11, b"\x01\x00"),
            _extension(13, b"\x00\x0c\x04\x03\x08\x04\x04\x01\x05\x03\x08\x05\x05\x01"),
            _extension(16, struct.pack(">H", len(protocols)) + protocols),
            _extension(23, b""),
            _extension(43, b"\x04\x03\x04\x03\x03"),
            _extension(45, b"\x01\x01"),
            _extension(51, b"\x00\x24\x00\x1d\x00\x20" + curve_key),
            _extension(65281, b"\x00"),
        ]
    )
    suites = bytes.fromhex("130113021303c02bc02fc02cc030cca9cca8c013c014009c009d002f0035")
    prefix = (
        b"\x03\x03" + bytes(32) + b"\x20" + session_id + struct.pack(">H", len(suites)) + suites + b"\x01\x00"
    )
    padding_size = 517 - (5 + 4 + len(prefix) + 2 + len(extensions) + 4)
    if padding_size >= 0:
        extensions += _extension(21, bytes(padding_size))
    body = prefix + struct.pack(">H", len(extensions)) + extensions
    handshake = b"\x01" + len(body).to_bytes(3, "big") + body
    hello = bytearray(b"\x16\x03\x01" + struct.pack(">H", len(handshake)) + handshake)
    digest = bytearray(hmac.new(secret.key, hello, hashlib.sha256).digest())
    now = int(time.time()) if timestamp is None else timestamp
    stamp = struct.pack("<I", now & 0xFFFFFFFF)
    for index, byte in enumerate(stamp):
        digest[28 + index] ^= byte
    hello[11:43] = digest
    return bytes(hello)


async def read_tls_record(reader, expected_type: int) -> bytes:
    header = await reader.readexactly(5)
    size = int.from_bytes(header[3:5], "big")
    if header[:3] != bytes([expected_type, 3, 3]) or not 0 < size <= TLS_MAX_RECORD:
        raise ValueError("Invalid FakeTLS record")
    return header + await reader.readexactly(size)


async def authenticate_server_hello(reader, secret: ProxySecret, hello: bytes) -> None:
    server_hello = await read_tls_record(reader, 22)
    if len(server_hello) < 76 or server_hello[5] != 2 or server_hello[9:11] != b"\x03\x03":
        raise ValueError("Invalid FakeTLS server hello")
    if server_hello[43] != 32 or server_hello[44:76] != hello[44:76]:
        raise ValueError("FakeTLS session ID mismatch")
    if await reader.readexactly(6) != CHANGE_CIPHER_SPEC:
        raise ValueError("Invalid FakeTLS ChangeCipherSpec")
    app_record = await read_tls_record(reader, 23)
    response = bytearray(server_hello + CHANGE_CIPHER_SPEC + app_record)
    received_hash = bytes(response[11:43])
    response[11:43] = bytes(32)
    expected_hash = hmac.new(secret.key, hello[11:43] + response, hashlib.sha256).digest()
    if not hmac.compare_digest(received_hash, expected_hash):
        raise ValueError("FakeTLS server authentication failed")


def wrap_tls_payload(payload: bytes, first: bool = False) -> bytes:
    if not 0 < len(payload) <= TLS_MAX_RECORD:
        raise ValueError("Invalid FakeTLS payload size")
    record = b"\x17\x03\x03" + struct.pack(">H", len(payload)) + payload
    return (CHANGE_CIPHER_SPEC if first else b"") + record
