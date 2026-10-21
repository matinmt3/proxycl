"""Independent local MTProto peer: RSA private key, DH and encrypted service replies."""

import asyncio
import hashlib
import os
import struct
import time

import pytest
from Crypto.Cipher import AES
from Crypto.PublicKey import RSA

from tester import telegram_keys
from tester import telegram_session as client


def tl(value):
    prefix = bytes([len(value)]) if len(value) < 254 else b"\xfe" + len(value).to_bytes(3, "little")
    return prefix + value + bytes(-(len(prefix) + len(value)) % 4)


def take(data, offset):
    size = data[offset]
    start = offset + 1
    if size == 254:
        size = int.from_bytes(data[start : start + 3], "little")
        start += 3
    return data[start : start + size], start + size + (-(start + size) % 4)


def xor(a, b):
    return bytes(x ^ y for x, y in zip(a, b))


def ige(data, key, iv, decrypt=False):
    cipher = AES.new(key, AES.MODE_ECB)
    previous_cipher, previous_plain = iv[:16], iv[16:]
    output = bytearray()
    for offset in range(0, len(data), 16):
        block = data[offset : offset + 16]
        if decrypt:
            plain = xor(cipher.decrypt(xor(block, previous_plain)), previous_cipher)
            previous_cipher, previous_plain = block, plain
            output.extend(plain)
        else:
            encrypted = xor(cipher.encrypt(xor(block, previous_cipher)), previous_plain)
            previous_cipher, previous_plain = encrypted, block
            output.extend(encrypted)
    return bytes(output)


def kdf(auth, msg_key, outgoing):
    x = 0 if outgoing else 8
    first = hashlib.sha256(msg_key + auth[x : x + 36]).digest()
    second = hashlib.sha256(auth[40 + x : 76 + x] + msg_key).digest()
    return first[:8] + second[8:24] + first[24:], second[:8] + first[8:24] + second[24:]


@pytest.fixture(scope="module")
def rsa_key():
    return RSA.generate(2048)


class Peer:
    def __init__(self, key, *, fault=None, container=False, response_delay=0):
        self.key, self.fault, self.container = key, fault, container
        self.replies = asyncio.Queue()
        self.server_nonce = os.urandom(16)
        self.pq = (499 * 547).to_bytes(3, "big")
        self.auth = None
        self.ping_count = 0
        self.destroyed = False
        self.last_id = 0
        self.sent_encrypted = []
        self.response_delay = response_delay
        self.ping_times = []
        self.received_client_ids = []
        self.acknowledged = []
        self.clear_ids = []
        self.server_content_count = 0

    def message_id(self):
        self.last_id = max((int(time.time() * (1 << 32)) & ~3) | 1, self.last_id + 4)
        return self.last_id

    async def receive(self):
        return await self.replies.get()

    async def clear_reply(self, body):
        if self.fault == "auth_padding":
            body += os.urandom(128)
        message_id = self.message_id()
        if self.fault == "clear_older" and self.clear_ids:
            message_id = self.clear_ids[0] - 4
        self.clear_ids.append(message_id)
        await self.replies.put(struct.pack("<QQI", 0, message_id, len(body)) + body)

    def fingerprint(self):
        return int.from_bytes(
            hashlib.sha1(tl(self.key.n.to_bytes(256, "big")) + tl(self.key.e.to_bytes(3, "big"))).digest()[
                -8:
            ],
            "little",
        )

    async def send(self, packet):
        if packet[:8] == bytes(8):
            body = packet[20:]
            kind = int.from_bytes(body[:4], "little")
            if kind == 0xBE7E8EF1:
                self.nonce = body[4:20]
                fingerprint = self.fingerprint() ^ (1 if self.fault == "rsa" else 0)
                nonce = bytes(16) if self.fault == "nonce" else self.nonce
                await self.clear_reply(
                    struct.pack("<I", 0x05162463)
                    + nonce
                    + self.server_nonce
                    + tl(self.pq)
                    + struct.pack("<IIQ", 0x1CB5C415, 1, fingerprint)
                )
            elif kind == 0xD712E4BE:
                assert body[4:36] == self.nonce + self.server_nonce
                p, end = take(body, 36)
                q, end = take(body, end)
                assert int.from_bytes(p, "big") * int.from_bytes(q, "big") == int.from_bytes(self.pq, "big")
                end += 8
                encrypted, _ = take(body, end)
                decoded = pow(int.from_bytes(encrypted, "big"), self.key.d, self.key.n).to_bytes(256, "big")
                temporary_key = xor(decoded[:32], hashlib.sha256(decoded[32:]).digest())
                padded = ige(decoded[32:], temporary_key, bytes(32), decrypt=True)
                inner = padded[:192][::-1]
                assert padded[192:] == hashlib.sha256(temporary_key + inner).digest()
                assert inner[:4] == struct.pack("<I", 0xA9F55F95)
                _, offset = take(inner, 4)
                _, offset = take(inner, offset)
                _, offset = take(inner, offset)
                assert inner[offset : offset + 32] == self.nonce + self.server_nonce
                self.new_nonce = inner[offset + 32 : offset + 64]
                assert struct.unpack_from("<i", inner, offset + 64)[0] == 2
                a = hashlib.sha1(self.new_nonce + self.server_nonce).digest()
                b = hashlib.sha1(self.server_nonce + self.new_nonce).digest()
                self.temporary_key = a + b[:12]
                self.temporary_iv = b[12:] + hashlib.sha1(self.new_nonce * 2).digest() + self.new_nonce[:4]
                self.exponent = int.from_bytes(os.urandom(256), "big")
                self.prime = telegram_keys.DH_PRIME
                public = pow(3, self.exponent, self.prime)
                if self.fault == "dh":
                    public = 1
                prime_bytes = (self.prime - 2 if self.fault == "unknown_prime" else self.prime).to_bytes(
                    256, "big"
                )
                generator = 9 if self.fault == "generator" else 3
                inner_nonce = bytes(16) if self.fault == "inner_nonce" else self.nonce
                answer = (
                    struct.pack("<I", 0xB5890DBA)
                    + inner_nonce
                    + self.server_nonce
                    + struct.pack("<i", generator)
                    + tl(prime_bytes)
                    + tl(public.to_bytes(256, "big"))
                    + struct.pack("<i", int(time.time()))
                )
                answer = hashlib.sha1(answer).digest() + answer
                if self.fault == "answer_hash":
                    answer = bytes(20) + answer[20:]
                answer += os.urandom(-len(answer) % 16)
                await self.clear_reply(
                    struct.pack("<I", 0xD0E8075C)
                    + self.nonce
                    + self.server_nonce
                    + tl(ige(answer, self.temporary_key, self.temporary_iv))
                )
            elif kind == 0xF5045F1F:
                encrypted, _ = take(body, 36)
                decoded = ige(encrypted, self.temporary_key, self.temporary_iv, decrypt=True)
                inner = decoded[20:]
                assert inner[:4] == struct.pack("<I", 0x6643B654)
                assert inner[4:36] == self.nonce + self.server_nonce
                public, end = take(inner, 44)
                assert decoded[:20] == hashlib.sha1(inner[:end]).digest()
                self.auth = pow(int.from_bytes(public, "big"), self.exponent, self.prime).to_bytes(256, "big")
                self.salt = xor(self.new_nonce[:8], self.server_nonce[:8])
                digest = hashlib.sha1(
                    self.new_nonce + b"\x01" + hashlib.sha1(self.auth).digest()[:8]
                ).digest()[-16:]
                if self.fault == "confirmation":
                    digest = bytes(16)
                await self.clear_reply(
                    struct.pack("<I", 0x3BCBF734) + self.nonce + self.server_nonce + digest
                )
            else:
                raise AssertionError(f"Unknown clear query {kind:x}")
        else:
            assert packet[:8] == hashlib.sha1(self.auth).digest()[-8:]
            key, iv = kdf(self.auth, packet[8:24], True)
            plain = ige(packet[24:], key, iv, decrypt=True)
            assert packet[8:24] == hashlib.sha256(self.auth[88:120] + plain).digest()[8:24]
            assert plain[:8] == self.salt
            self.session = plain[8:16]
            request_id, sequence, size = struct.unpack_from("<QII", plain, 16)
            self.received_client_ids.append(request_id)
            body = plain[32 : 32 + size]
            kind = int.from_bytes(body[:4], "little")
            if kind == 0x7ABE77EC:
                self.ping_count += 1
                self.ping_times.append(time.monotonic())
                if self.response_delay:
                    await asyncio.sleep(self.response_delay)
                cookie = body[4:12]
                if self.fault == "ping_timeout":
                    return
                if self.fault == "salt_recovery" and self.ping_count == 1:
                    self.salt = os.urandom(8)
                    await self.encrypted_reply(
                        struct.pack("<IQII", 0xEDAB447B, request_id, sequence, 48) + self.salt
                    )
                    return
                if self.fault == "new_session" and self.ping_count == 1:
                    self.salt = os.urandom(8)
                    notification = struct.pack("<IQ", 0x9EC20908, request_id) + os.urandom(8) + self.salt
                    await self.encrypted_reply(notification, sequence=1)
                if self.fault == "pong":
                    cookie = bytes(8)
                pong_request = request_id + 4 if self.fault == "pong_request" else request_id
                response = struct.pack("<IQ", 0x347773C5, pong_request) + cookie
                await self.encrypted_reply(response)
            elif kind == 0xD1435160:
                self.destroyed = True
                if self.fault == "cleanup_timeout":
                    return
                await self.encrypted_reply(struct.pack("<I", 0xF660E1D4))
            elif kind == 0x62D6B459:
                assert body[4:8] == struct.pack("<I", 0x1CB5C415)
                count = struct.unpack_from("<I", body, 8)[0]
                self.acknowledged.extend(struct.unpack_from("<Q", body, 12 + 8 * i)[0] for i in range(count))
            else:
                raise AssertionError(f"Unknown encrypted query {kind:x}")

    async def encrypted_reply(self, body, sequence=None):
        message_id = self.message_id()
        sequence = (
            sequence
            if sequence is not None
            else 2 * self.server_content_count + int(self.fault == "content_pong" and not self.destroyed)
        )
        self.server_content_count = max(self.server_content_count, (sequence + 1) // 2)
        if self.fault == "sequence_regression" and not self.destroyed:
            sequence = 2 if not self.sent_encrypted else 0
        if self.container and not self.destroyed:
            inner_sequence = 2 if self.fault == "container_sequence" else sequence
            body = struct.pack("<IIQII", 0x73F1F8DC, 1, message_id, inner_sequence, len(body)) + body
            message_id = self.message_id()
            sequence = 2 * self.server_content_count
        if self.fault == "time":
            message_id -= 1000 * (1 << 32)
        if self.fault == "older" and self.sent_encrypted and not self.destroyed:
            message_id = self.first_server_id - 4
        if not self.sent_encrypted:
            self.first_server_id = message_id
        salt = bytes(8) if self.fault == "salt" else self.salt
        session = bytes(8) if self.fault == "session" else self.session
        if self.fault == "negative_sequence":
            sequence = 0xFFFFFFFF
        claimed_size = len(body) + (4 if self.fault == "padding" else 0)
        plain = salt + session + struct.pack("<QII", message_id, sequence, claimed_size) + body
        plain += os.urandom(12 + (-(len(plain) + 12) % 16))
        msg_key = hashlib.sha256(self.auth[96:128] + plain).digest()[8:24]
        key, iv = kdf(self.auth, msg_key, False)
        packet = hashlib.sha1(self.auth).digest()[-8:] + msg_key + ige(plain, key, iv)
        if self.fault == "auth_id" and not self.destroyed:
            packet = bytes(8) + packet[8:]
        if self.fault == "msg_key" and not self.destroyed:
            packet = packet[:8] + bytes(16) + packet[24:]
        if self.fault == "replay" and self.sent_encrypted and not self.destroyed:
            packet = self.sent_encrypted[0]
        self.sent_encrypted.append(packet)
        await self.replies.put(packet)


def peer(monkeypatch, key, **kwargs):
    result = Peer(key, **kwargs)
    monkeypatch.setattr(telegram_keys, "RSA_PUBLIC_KEYS", {result.fingerprint(): (key.n, key.e)})
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("container", [False, True])
async def test_full_authenticated_session_matches_three_pongs_and_destroys_its_key(
    monkeypatch, rsa_key, container
):
    remote = peer(monkeypatch, rsa_key, container=container)
    result = await client.verify_session(remote.send, remote.receive, 2, ping_interval=0)
    assert result.authenticated is True and len(result.ping_rtts_ms) == 3
    assert all(value >= 0 for value in result.ping_rtts_ms)
    assert result.handshake_ms > 0 and remote.ping_count == 3 and remote.destroyed
    assert result.cleanup_confirmed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "rsa",
        "nonce",
        "dh",
        "unknown_prime",
        "generator",
        "inner_nonce",
        "answer_hash",
        "confirmation",
        "msg_key",
        "auth_id",
        "pong",
        "pong_request",
        "session",
        "salt",
        "replay",
        "time",
        "padding",
        "negative_sequence",
    ],
)
async def test_no_authenticated_success_for_invalid_proofs_or_session_messages(monkeypatch, rsa_key, fault):
    remote = peer(monkeypatch, rsa_key, fault=fault)
    with pytest.raises(ValueError):
        await client.verify_session(remote.send, remote.receive, 2, ping_interval=0)
    if fault not in ("rsa", "nonce", "dh", "unknown_prime", "generator", "inner_nonce", "answer_hash"):
        assert remote.auth is not None  # The targeted later phase was reached.


@pytest.mark.asyncio
async def test_auth_timeout_and_cancellation_are_not_converted_to_success(monkeypatch, rsa_key):
    remote = peer(monkeypatch, rsa_key)

    async def stalled():
        await asyncio.Future()

    with pytest.raises(TimeoutError):
        await client.verify_session(remote.send, stalled, 2, auth_timeout=0.02)
    task = asyncio.create_task(client.verify_session(remote.send, stalled, 2))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_factorization_rejects_prime_or_unbounded_input():
    for value in (1, 17, 1 << 100, 49):
        with pytest.raises(ValueError):
            client._factor_pq(value, time.monotonic() + 0.1)
    assert client._factor_pq(499 * 547, time.monotonic() + 0.1) == (499, 547)


def test_trust_anchors_match_official_telegram_values():
    assert 0xD09D1D85DE64FD85 in telegram_keys.RSA_PUBLIC_KEYS
    assert telegram_keys.DH_PRIME.bit_length() == 2048
    assert (
        hashlib.sha256(telegram_keys.DH_PRIME.to_bytes(256, "big")).hexdigest()
        == "02f85e7687fc6f33ba678226a963b3c8a191b47c890cf30debe17c1d623b5af1"
    )


@pytest.mark.asyncio
async def test_ping_latency_includes_sending_backpressure_and_replies(monkeypatch, rsa_key):
    remote = peer(monkeypatch, rsa_key, response_delay=0.03)
    result = await client.verify_session(remote.send, remote.receive, 2, ping_interval=0.02)
    assert all(value >= 29 for value in result.ping_rtts_ms)
    assert all(second - first >= 0.049 for first, second in zip(remote.ping_times, remote.ping_times[1:]))


@pytest.mark.asyncio
async def test_authenticated_server_clock_starts_new_encrypted_message_id_sequence(monkeypatch, rsa_key):
    remote = peer(monkeypatch, rsa_key)
    original = client._Session.__init__

    def skewed(self, send, receive, auth, ids):
        # Unencrypted authentication used a host clock one hour in the future.
        ids.previous = int((time.time() + 3600) * (1 << 32)) & ~3
        original(self, send, receive, auth, ids)

    monkeypatch.setattr(client._Session, "__init__", skewed)
    result = await client.verify_session(remote.send, remote.receive, 2, ping_interval=0)
    assert result.authenticated
    assert all(value / (1 << 32) < time.time() + 30 for value in remote.received_client_ids)


@pytest.mark.asyncio
async def test_valid_content_related_pongs_are_acknowledged(monkeypatch, rsa_key):
    # Official description permits any constructor except ack/containers to be
    # content-related for reliability; rejecting every odd-seq pong is wrong.
    remote = peer(monkeypatch, rsa_key, fault="content_pong")
    result = await client.verify_session(remote.send, remote.receive, 2, ping_interval=0)
    assert result.authenticated and len(remote.acknowledged) == 3


@pytest.mark.asyncio
async def test_official_style_handshake_object_padding_is_tolerated(monkeypatch, rsa_key):
    remote = peer(monkeypatch, rsa_key, fault="auth_padding")
    result = await client.verify_session(remote.send, remote.receive, 2, ping_interval=0)
    assert result.authenticated and result.cleanup_confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["older", "clear_older", "sequence_regression", "container_sequence"])
async def test_old_unique_message_ids_and_inconsistent_sequences_are_rejected(monkeypatch, rsa_key, fault):
    remote = peer(monkeypatch, rsa_key, fault=fault, container=fault == "container_sequence")
    with pytest.raises(ValueError):
        await client.verify_session(remote.send, remote.receive, 2, ping_interval=0)


def test_factorization_handles_real_telegram_challenge_with_a_bounded_budget():
    assert client._factor_pq(3358800871349344843, time.monotonic() + 2) == (1786331737, 1880278339)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["salt_recovery", "new_session"])
async def test_authenticated_salt_updates_are_processed_before_matching_pongs(monkeypatch, rsa_key, fault):
    remote = peer(monkeypatch, rsa_key, fault=fault)
    result = await client.verify_session(remote.send, remote.receive, 2, ping_interval=0)
    assert len(result.ping_rtts_ms) == 3 and remote.destroyed
    assert remote.ping_count == (4 if fault == "salt_recovery" else 3)
    if fault == "new_session":
        assert len(remote.acknowledged) == 1


@pytest.mark.asyncio
async def test_ping_timeout_and_mid_ping_cancellation_propagate_after_cleanup(monkeypatch, rsa_key):
    remote = peer(monkeypatch, rsa_key, fault="ping_timeout")
    with pytest.raises(TimeoutError):
        await client.verify_session(remote.send, remote.receive, 2, ping_timeout=0.03)
    assert remote.destroyed
    remote = peer(monkeypatch, rsa_key, fault="ping_timeout")
    task = asyncio.create_task(client.verify_session(remote.send, remote.receive, 2))
    while not remote.ping_count:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert remote.destroyed


@pytest.mark.asyncio
async def test_cleanup_failure_is_bounded_and_reported_without_claiming_deletion(monkeypatch, rsa_key):
    remote = peer(monkeypatch, rsa_key, fault="cleanup_timeout")
    started = time.monotonic()
    result = await asyncio.wait_for(client.verify_session(remote.send, remote.receive, 2, ping_interval=0), 3)
    assert result.authenticated and not result.cleanup_confirmed
    assert time.monotonic() - started < 3


@pytest.mark.parametrize("blob", [b"\xff", b"\xfe\x01\x00\x00x\x00\x00\x00", b"\x05ab"])
def test_tl_reader_rejects_malformed_or_noncanonical_lengths(blob):
    with pytest.raises(ValueError):
        client._Reader(blob).blob()


@pytest.mark.parametrize(
    "options",
    [
        {"dc_id": 0},
        {"dc_id": True},
        {"ping_count": 0},
        {"ping_count": True},
        {"auth_timeout": float("inf")},
        {"ping_interval": -1},
        {"ping_timeout": 0},
    ],
)
@pytest.mark.asyncio
async def test_bad_public_options_fail_before_network(options):
    async def forbidden(*args):
        pytest.fail("Network used before option validation")

    options = {"dc_id": 2, **options}
    with pytest.raises(ValueError):
        await client.verify_session(forbidden, forbidden, **options)
