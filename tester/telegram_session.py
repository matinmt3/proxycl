"""Account-free, RSA-authenticated Telegram MTProto 2.0 service-session probe.

Protocol: https://core.telegram.org/mtproto/auth_key,
https://core.telegram.org/mtproto/description and /mtproto/service_messages.
Reference implementation: TDLib 027780b87a66a2a0e69db644e812362bb300f1e1,
td/mtproto/{Handshake,DhHandshake,PingConnection,AuthData}.cpp.

Only base MTProto services are used: no API credentials, login or user data.
The fresh main auth key stays in RAM. A bounded destroy_auth_key is attempted;
a lost connection can prevent confirmation of server-side key deletion.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import os
import secrets
import struct
import threading
import time
from dataclasses import dataclass
from math import gcd

from Crypto.Cipher import AES

from tester import telegram_keys

MAX_PACKET = 65536
MAX_CONTROL_PACKETS = 32
REQ_PQ, RES_PQ = 0xBE7E8EF1, 0x05162463
REQ_DH, DH_PARAMS = 0xD712E4BE, 0xD0E8075C
SET_DH, DH_INNER, CLIENT_DH = 0xF5045F1F, 0xB5890DBA, 0x6643B654
DH_OK, DH_RETRY, DH_FAIL = 0x3BCBF734, 0x46DC1FB9, 0xA69DAE02
PING, PONG = 0x7ABE77EC, 0x347773C5
CONTAINER, ACK, VECTOR = 0x73F1F8DC, 0x62D6B459, 0x1CB5C415
NEW_SESSION, BAD_SALT, BAD_MESSAGE = 0x9EC20908, 0xEDAB447B, 0xA7EFF811
DESTROY, DESTROY_OK, DESTROY_NONE = 0xD1435160, 0xF660E1D4, 0x0A9F2259
RPC_RESULT = 0xF35C6D01


@dataclass
class SessionVerification:
    ping_rtts_ms: list[float]
    handshake_ms: float
    authenticated: bool = True
    cleanup_confirmed: bool = False


class VerificationError(ValueError):
    def __init__(self, message: str, stage: str = "auth"):
        self.stage = stage
        super().__init__(message)


class _Reader:
    def __init__(self, data: bytes):
        self.data, self.offset = data, 0

    def take(self, size):
        if size < 0 or self.offset + size > len(self.data):
            raise ValueError("Truncated MTProto object")
        result = self.data[self.offset : self.offset + size]
        self.offset += size
        return result

    def uint(self):
        return int.from_bytes(self.take(4), "little")

    def integer(self):
        return int.from_bytes(self.take(4), "little", signed=True)

    def long(self):
        return int.from_bytes(self.take(8), "little")

    def blob(self, maximum=4096):
        size = self.take(1)[0]
        prefix = 1
        if size == 254:
            size, prefix = int.from_bytes(self.take(3), "little"), 4
            if size < 254:
                raise ValueError("Noncanonical TL byte length")
        elif size == 255:
            raise ValueError("Invalid TL byte length")
        if size > maximum:
            raise ValueError("Oversized TL object")
        value = self.take(size)
        self.take(-(size + prefix) % 4)
        return value

    def end(self, padding=0):
        if not 0 <= len(self.data) - self.offset <= padding:
            raise ValueError("Trailing MTProto object data")


def _tl(value: bytes) -> bytes:
    prefix = bytes([len(value)]) if len(value) < 254 else b"\xfe" + len(value).to_bytes(3, "little")
    return prefix + value + bytes(-(len(prefix) + len(value)) % 4)


def _integer(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _xor(first, second):
    return bytes(a ^ b for a, b in zip(first, second))


def _ige(data: bytes, key: bytes, iv: bytes, *, decrypt=False) -> bytes:
    if not data or len(data) % 16 or len(data) > MAX_PACKET or len(key) != 32 or len(iv) != 32:
        raise ValueError("Invalid AES-IGE parameters")
    cipher = AES.new(key, AES.MODE_ECB)
    previous_cipher, previous_plain = iv[:16], iv[16:]
    output = bytearray()
    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16]
        if decrypt:
            plain = _xor(cipher.decrypt(_xor(block, previous_plain)), previous_cipher)
            previous_cipher, previous_plain = block, plain
            output.extend(plain)
        else:
            encrypted = _xor(cipher.encrypt(_xor(block, previous_cipher)), previous_plain)
            previous_cipher, previous_plain = encrypted, block
            output.extend(encrypted)
    return bytes(output)


def _is_prime(value):
    if value < 2:
        return False
    for prime in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if value % prime == 0:
            return value == prime
    odd, exponent = value - 1, 0
    while odd % 2 == 0:
        odd //= 2
        exponent += 1
    # Deterministic Miller-Rabin bases for unsigned 64-bit integers.
    for base in (2, 325, 9375, 28178, 450775, 9780504, 1795265022):
        if base % value == 0:
            continue
        result = pow(base, odd, value)
        if result in (1, value - 1):
            continue
        for _ in range(exponent - 1):
            result = result * result % value
            if result == value - 1:
                break
        else:
            return False
    return True


def _factor_pq(value: int, deadline: float, cancelled=None) -> tuple[int, int]:
    if type(value) is not int or not 15 <= value < 1 << 63 or value % 2 == 0 or _is_prime(value):
        raise ValueError("Unsupported pq challenge")
    # Brent batching reduces expensive gcd calls; both iterations and wall time
    # are bounded. Cancellation signals the worker rather than leaving CPU work.
    steps = 0

    def tick():
        nonlocal steps
        steps += 1
        if (
            steps > 1000000
            or steps % 64 == 0
            and (time.monotonic() >= deadline or cancelled is not None and cancelled.is_set())
        ):
            raise TimeoutError("pq factorization budget exceeded")

    for _ in range(12):
        y, constant = secrets.randbelow(value - 2) + 2, secrets.randbelow(value - 1) + 1
        divisor, width, product = 1, 1, 1
        while divisor == 1:
            x = y
            for _ in range(width):
                y = (y * y + constant) % value
                tick()
            offset = 0
            while offset < width and divisor == 1:
                saved_y = y
                for _ in range(min(128, width - offset)):
                    y = (y * y + constant) % value
                    product = product * abs(x - y) % value
                    tick()
                divisor = gcd(product, value)
                offset += 128
            width *= 2
        if divisor == value:
            while True:
                saved_y = (saved_y * saved_y + constant) % value
                divisor = gcd(abs(x - saved_y), value)
                tick()
                if divisor > 1:
                    break
        if 1 < divisor < value:
            first, second = sorted((divisor, value // divisor))
            if first != second and first % 2 and second % 2 and _is_prime(first) and _is_prime(second):
                return first, second
            raise ValueError("pq is not a pair of distinct odd primes")
    raise TimeoutError("pq factorization iteration budget exceeded")


def _rsa_pad(data: bytes, n: int, e: int) -> bytes:
    if len(data) > 144 or n.bit_length() != 2048 or e < 3:
        raise ValueError("Invalid RSA trust anchor or payload")
    padded = data + os.urandom(192 - len(data))
    for _ in range(32):
        key = os.urandom(32)
        encrypted = _ige(padded[::-1] + hashlib.sha256(key + padded).digest(), key, bytes(32))
        block = _xor(key, hashlib.sha256(encrypted).digest()) + encrypted
        value = int.from_bytes(block, "big")
        if value < n:
            return pow(value, e, n).to_bytes(256, "big")
    raise ValueError("RSA padding rejection budget exceeded")


def _dh_values(prime, generator, public):
    if prime != telegram_keys.DH_PRIME:
        raise ValueError("Unrecognized Telegram DH prime")
    allowed = {
        2: prime % 8 == 7,
        3: prime % 3 == 2,
        4: True,
        5: prime % 5 in (1, 4),
        6: prime % 24 in (19, 23),
        7: prime % 7 in (3, 5, 6),
    }
    boundary = 1 << 1984
    if not allowed.get(generator, False) or not boundary <= public <= prime - boundary:
        raise ValueError("Unsafe Telegram DH parameters")
    exponent = int.from_bytes(os.urandom(256), "big")
    outbound = pow(generator, exponent, prime)
    if not boundary <= outbound <= prime - boundary:
        raise ValueError("Unsafe client DH public value")
    key = pow(public, exponent, prime).to_bytes(256, "big")
    return outbound, key


class _MessageIds:
    def __init__(self):
        self.previous = 0

    def next(self, now=None):
        value = int((time.time() if now is None else now) * (1 << 32)) & ~3
        self.previous = max(value, self.previous + 4)
        return self.previous


async def _clear_reply(receive, ids_seen):
    packet = await receive()
    if not isinstance(packet, bytes) or not 24 <= len(packet) <= MAX_PACKET:
        raise ValueError("Invalid unencrypted MTProto packet size")
    auth_id, message_id, size = struct.unpack_from("<QQI", packet)
    if (
        auth_id
        or message_id % 2 != 1
        or message_id in ids_seen
        or ids_seen
        and message_id < min(ids_seen)
        or size % 4
        or size != len(packet) - 20
    ):
        raise ValueError("Invalid unencrypted MTProto envelope")
    ids_seen.add(message_id)
    return _Reader(packet[20:])


@dataclass
class _Auth:
    key: bytes
    salt: bytes
    server_time: int
    synchronized_at: float


async def _authenticate(send, receive, dc_id, ids, deadline):
    nonce, new_nonce, seen = os.urandom(16), os.urandom(32), set()

    async def send_clear(body):
        await send(struct.pack("<QQI", 0, ids.next(), len(body)) + body)

    await send_clear(struct.pack("<I", REQ_PQ) + nonce)
    reply = await _clear_reply(receive, seen)
    if reply.uint() != RES_PQ or reply.take(16) != nonce:
        raise ValueError("Invalid req_pq nonce or response")
    server_nonce, pq = reply.take(16), reply.blob(8)
    if not pq or pq[0] == 0 or reply.uint() != VECTOR:
        raise ValueError("Invalid pq challenge")
    count = reply.uint()
    if not 1 <= count <= 64:
        raise ValueError("Invalid RSA fingerprint count")
    fingerprints = [reply.long() for _ in range(count)]
    reply.end(padding=255)
    selected = next((value for value in fingerprints if value in telegram_keys.RSA_PUBLIC_KEYS), None)
    if selected is None:
        raise ValueError("No pinned Telegram RSA fingerprint")
    cancelled = threading.Event()
    try:
        p, q = await asyncio.to_thread(
            _factor_pq, int.from_bytes(pq, "big"), min(deadline, time.monotonic() + 2.0), cancelled
        )
    except asyncio.CancelledError:
        cancelled.set()
        raise
    inner = struct.pack("<I", 0xA9F55F95) + _tl(pq) + _tl(_integer(p)) + _tl(_integer(q))
    inner += nonce + server_nonce + new_nonce + struct.pack("<i", dc_id)
    n, e = telegram_keys.RSA_PUBLIC_KEYS[selected]
    encrypted = await asyncio.to_thread(_rsa_pad, inner, n, e)
    await send_clear(
        struct.pack("<I", REQ_DH)
        + nonce
        + server_nonce
        + _tl(_integer(p))
        + _tl(_integer(q))
        + struct.pack("<Q", selected)
        + _tl(encrypted)
    )
    reply = await _clear_reply(receive, seen)
    if reply.uint() != DH_PARAMS or reply.take(16) != nonce or reply.take(16) != server_nonce:
        raise ValueError("Invalid server DH nonces or response")
    encrypted = reply.blob(4096)
    reply.end(padding=255)
    first = hashlib.sha1(new_nonce + server_nonce).digest()
    second = hashlib.sha1(server_nonce + new_nonce).digest()
    temp_key = first + second[:12]
    temp_iv = second[12:] + hashlib.sha1(new_nonce + new_nonce).digest() + new_nonce[:4]
    decoded = await asyncio.to_thread(_ige, encrypted, temp_key, temp_iv, decrypt=True)
    inner = _Reader(decoded[20:])
    if inner.uint() != DH_INNER or inner.take(16) != nonce or inner.take(16) != server_nonce:
        raise ValueError("Invalid encrypted server DH identity")
    generator = inner.integer()
    prime, public = int.from_bytes(inner.blob(256), "big"), int.from_bytes(inner.blob(256), "big")
    server_time = inner.integer()
    synchronized_at = time.monotonic()
    if not 0 <= len(inner.data) - inner.offset <= 15 or not hmac.compare_digest(
        decoded[:20], hashlib.sha1(inner.data[: inner.offset]).digest()
    ):
        raise ValueError("Invalid server DH answer hash/padding")
    retry_id = 0
    for _ in range(3):
        outbound, key = await asyncio.to_thread(_dh_values, prime, generator, public)
        client_inner = (
            struct.pack("<I", CLIENT_DH)
            + nonce
            + server_nonce
            + struct.pack("<Q", retry_id)
            + _tl(_integer(outbound))
        )
        payload = hashlib.sha1(client_inner).digest() + client_inner
        payload += os.urandom(-len(payload) % 16)
        encrypted = await asyncio.to_thread(_ige, payload, temp_key, temp_iv)
        await send_clear(struct.pack("<I", SET_DH) + nonce + server_nonce + _tl(encrypted))
        reply = await _clear_reply(receive, seen)
        kind = reply.uint()
        if (
            kind not in (DH_OK, DH_RETRY, DH_FAIL)
            or reply.take(16) != nonce
            or reply.take(16) != server_nonce
        ):
            raise ValueError("Invalid DH confirmation identity")
        proof = reply.take(16)
        reply.end(padding=255)
        aux_hash = hashlib.sha1(key).digest()[:8]
        marker = {DH_OK: 1, DH_RETRY: 2, DH_FAIL: 3}[kind]
        expected = hashlib.sha1(new_nonce + bytes([marker]) + aux_hash).digest()[-16:]
        if not hmac.compare_digest(proof, expected):
            raise ValueError("Invalid DH confirmation proof")
        if kind == DH_OK:
            return _Auth(key, _xor(new_nonce[:8], server_nonce[:8]), server_time, synchronized_at)
        if kind == DH_FAIL:
            raise ValueError("Telegram rejected DH exchange")
        retry_id = int.from_bytes(aux_hash, "little")
    raise ValueError("DH retry budget exceeded")


def _kdf(key, msg_key, x):
    first = hashlib.sha256(msg_key + key[x : x + 36]).digest()
    second = hashlib.sha256(key[40 + x : 76 + x] + msg_key).digest()
    return first[:8] + second[8:24] + first[24:32], second[:8] + first[8:24] + second[24:32]


class _Session:
    def __init__(self, send, receive, auth, ids):
        self.send, self.receive, self.auth, self.ids = send, receive, auth, ids
        # Authentication has no encrypted session. Start its new ID sequence
        # from the authenticated server clock, even when the host clock is wrong.
        self.ids.previous = 0
        self.session_id = os.urandom(8)
        self.seen, self.sent = set(), set()
        self.sequences = {}
        self.content_sent = 0
        self.first_message = None

    def now(self):
        return self.auth.server_time + time.monotonic() - self.auth.synchronized_at

    async def send_message(self, body, *, content=False):
        message_id = self.ids.next(self.now())
        sequence = 2 * self.content_sent + int(content)
        self.content_sent += int(content)
        plain = self.auth.salt + self.session_id + struct.pack("<QII", message_id, sequence, len(body)) + body
        plain += os.urandom(12 + (-(len(plain) + 12) % 16))
        msg_key = hashlib.sha256(self.auth.key[88:120] + plain).digest()[8:24]
        key, iv = _kdf(self.auth.key, msg_key, 0)
        encrypted = await asyncio.to_thread(_ige, plain, key, iv)
        await self.send(hashlib.sha1(self.auth.key).digest()[-8:] + msg_key + encrypted)
        self.sent.add(message_id)
        if self.first_message is None:
            self.first_message = message_id
        return message_id, sequence

    def check_id(self, value, minimum=0):
        timestamp = value / (1 << 32)
        if (
            value % 2 != 1
            or value in self.seen
            or value < minimum
            or not self.now() - 300 <= timestamp <= self.now() + 30
        ):
            raise ValueError("Invalid server message time, parity or replay")
        self.seen.add(value)
        if len(self.seen) > 1024:
            raise ValueError("Session message budget exceeded")

    async def read_messages(self):
        packet = await self.receive()
        if not isinstance(packet, bytes) or not 72 <= len(packet) <= MAX_PACKET or (len(packet) - 24) % 16:
            raise ValueError("Invalid encrypted MTProto packet size")
        if not hmac.compare_digest(packet[:8], hashlib.sha1(self.auth.key).digest()[-8:]):
            raise ValueError("Wrong authorization key ID")
        key, iv = _kdf(self.auth.key, packet[8:24], 8)
        plain = await asyncio.to_thread(_ige, packet[24:], key, iv, decrypt=True)
        expected = hashlib.sha256(self.auth.key[96:128] + plain).digest()[8:24]
        if not hmac.compare_digest(expected, packet[8:24]):
            raise ValueError("Invalid MTProto2 message key")
        if plain[8:16] != self.session_id:
            raise ValueError("Wrong MTProto session ID")
        message_id, sequence, size = struct.unpack_from("<QII", plain, 16)
        if sequence > 0x7FFFFFFF or size < 4 or size % 4 or not 12 <= len(plain) - 32 - size <= 1024:
            raise ValueError("Invalid MTProto message body or padding")
        minimum = min(self.seen, default=0)
        self.check_id(message_id, minimum)
        body = plain[32 : 32 + size]
        records = [(message_id, sequence, body)]
        if int.from_bytes(body[:4], "little") == CONTAINER:
            if sequence % 2:
                raise ValueError("Content-related container")
            reader = _Reader(body)
            reader.uint()
            count = reader.uint()
            if count > 64:
                raise ValueError("Container message budget exceeded")
            records = []
            for _ in range(count):
                inner_id, inner_sequence, inner_size = reader.long(), reader.uint(), reader.uint()
                if inner_id >= message_id or inner_sequence > sequence or inner_size < 4 or inner_size % 4:
                    raise ValueError("Invalid container entry")
                self.check_id(inner_id, minimum)
                inner_body = reader.take(inner_size)
                if int.from_bytes(inner_body[:4], "little") == CONTAINER:
                    raise ValueError("Nested MTProto container")
                records.append((inner_id, inner_sequence, inner_body))
            reader.end()
        sequence_updates = {value: seq for value, seq, _ in records}
        sequence_updates[message_id] = sequence
        ordered = sorted({**self.sequences, **sequence_updates}.items())
        if any(first[1] > second[1] for first, second in zip(ordered, ordered[1:])):
            raise ValueError("Inconsistent server message sequence numbers")
        self.sequences.update(sequence_updates)
        return plain[:8], records

    async def ping(self):
        cookie = os.urandom(8)
        query = struct.pack("<I", PING) + cookie
        started = time.monotonic()
        pending, sequence = await self.send_message(query)
        retries = 0
        for _ in range(MAX_CONTROL_PACKETS):
            envelope_salt, records = await self.read_messages()
            proposed_salt, resend, matched = None, False, False
            ack_ids = []
            server_pings = []
            corrected_time = None
            for message_id, seq_no, body in records:
                reader = _Reader(body)
                kind = reader.uint()
                if kind == ACK and seq_no % 2:
                    raise ValueError("Content-related acknowledgement")
                if seq_no % 2:
                    ack_ids.append(message_id)
                if kind == PONG:
                    if reader.long() != pending or reader.take(8) != cookie or matched:
                        raise ValueError("Pong does not match the outstanding ping")
                    matched = True
                elif kind == NEW_SESSION:
                    first = reader.long()
                    reader.long()
                    salt = reader.take(8)
                    if first != self.first_message:
                        raise ValueError("Unrelated new-session notification")
                    if proposed_salt is not None and proposed_salt != salt:
                        raise ValueError("Conflicting session salts")
                    proposed_salt = salt
                    if message_id not in ack_ids:
                        ack_ids.append(message_id)
                elif kind == BAD_SALT:
                    if reader.long() != pending or reader.uint() != sequence or reader.uint() != 48:
                        raise ValueError("Unrelated bad-server-salt notification")
                    salt = reader.take(8)
                    if proposed_salt is not None and proposed_salt != salt:
                        raise ValueError("Conflicting session salts")
                    proposed_salt, resend = salt, True
                elif kind == BAD_MESSAGE:
                    if reader.long() != pending or reader.uint() != sequence or reader.uint() not in (16, 17):
                        raise ValueError("Unrecoverable bad-message notification")
                    # Only an authenticated notification may adjust the clock.
                    corrected_time = int(message_id >> 32)
                    resend = True
                elif kind == ACK:
                    if reader.uint() != VECTOR:
                        raise ValueError("Invalid acknowledgement vector")
                    count = reader.uint()
                    if count > 128:
                        raise ValueError("Oversized acknowledgement vector")
                    for _ in range(count):
                        if reader.long() not in self.sent:
                            raise ValueError("Acknowledgement for an unknown request")
                elif kind == PING:
                    server_pings.append((message_id, reader.take(8)))
                else:
                    raise ValueError("Unexpected MTProto service response")
                reader.end()
            if envelope_salt != self.auth.salt and envelope_salt != proposed_salt:
                raise ValueError("Wrong MTProto server salt")
            if proposed_salt is not None:
                self.auth.salt = proposed_salt
            if corrected_time is not None:
                self.auth.server_time, self.auth.synchronized_at = corrected_time, time.monotonic()
            for message_id, incoming_cookie in server_pings:
                await self.send_message(struct.pack("<IQ", PONG, message_id) + incoming_cookie)
            if ack_ids:
                await self.send_message(
                    struct.pack("<III", ACK, VECTOR, len(ack_ids))
                    + b"".join(struct.pack("<Q", value) for value in ack_ids)
                )
            if resend:
                retries += 1
                if retries > 2 or matched:
                    raise ValueError("Service-recovery budget exceeded")
                pending, sequence = await self.send_message(query)
            elif matched:
                return (time.monotonic() - started) * 1000
        raise ValueError("No pong within the control-message budget")

    async def cleanup(self):
        message_id, _ = await self.send_message(struct.pack("<I", DESTROY), content=True)
        for _ in range(4):
            salt, records = await self.read_messages()
            if salt != self.auth.salt:
                raise ValueError("Wrong cleanup salt")
            for _, _, body in records:
                reader = _Reader(body)
                kind = reader.uint()
                if kind == RPC_RESULT:
                    if reader.long() != message_id:
                        raise ValueError("Unrelated cleanup response")
                    kind = reader.uint()
                if kind in (DESTROY_OK, DESTROY_NONE):
                    reader.end()
                    return True
        return False


async def verify_session(
    send_packet,
    receive_packet,
    dc_id,
    *,
    ping_count=3,
    ping_interval=0.5,
    auth_timeout=12.0,
    ping_timeout=3.0,
) -> SessionVerification:
    """Authenticate Telegram and require every requested encrypted ping to match.

    The async callbacks exchange raw MTProto packets, without transport framing.
    Timeouts and cancellation propagate. Auth keys are neither returned nor saved.
    """
    if type(dc_id) is not int or dc_id not in range(1, 6):
        raise ValueError("Unsupported production DC")
    if type(ping_count) is not int or not 1 <= ping_count <= 10:
        raise ValueError("ping_count must be between 1 and 10")
    for value, minimum, maximum in ((ping_interval, 0, 5), (auth_timeout, 0, 120), (ping_timeout, 0, 60)):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not minimum <= value <= maximum
        ):
            raise ValueError("Invalid session timing option")
    if auth_timeout <= 0 or ping_timeout <= 0:
        raise ValueError("Session timeouts must be positive")
    ids, session, result = _MessageIds(), None, None
    started = time.monotonic()
    try:
        async with asyncio.timeout(auth_timeout):
            auth = await _authenticate(send_packet, receive_packet, dc_id, ids, started + auth_timeout)
        handshake_ms = (time.monotonic() - started) * 1000
        session = _Session(send_packet, receive_packet, auth, ids)
        rtts = []
        for index in range(ping_count):
            if index:
                await asyncio.sleep(ping_interval)
            async with asyncio.timeout(ping_timeout):
                rtts.append(await session.ping())
        result = SessionVerification(rtts, handshake_ms)
        return result
    finally:
        if session is not None:
            try:
                async with asyncio.timeout(0.75):
                    confirmed = await session.cleanup()
                    if result is not None:
                        result.cleanup_confirmed = confirmed
            except (ValueError, TimeoutError, OSError, asyncio.IncompleteReadError):
                pass
