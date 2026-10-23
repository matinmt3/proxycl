"""Optional media adapter tests use synthetic data and never log in to Telegram."""

import asyncio
import hashlib
import json
import logging
import socket
import ssl
import struct
import time
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from media_test import MediaSettings
from media_test.models import CHUNK_BYTES, BudgetExceeded, CdnUnsupported, parse_public_message_link
from media_test.report import read_report, safe_report, write_report
from media_test.runner import failure_status, measure_round, run_media_test_async, select_candidates
from media_test.transport import CountingReader, TransferBudget, connection_factory
from tester.faketls import CHANGE_CIPHER_SPEC, TLS_MAX_RECORD, wrap_tls_stream
from tester.mtproto import MAX_PROBE_RESPONSE, _connection_failure_reason, _read_frame, _SessionTransport
from tester.secrets import decode_secret
from utils.models import FailureReason, Proxy, ProxyResult, TestSample


def qualified(server="proxy.example", *, age=0):
    return ProxyResult(
        Proxy(server, 443, "12" * 16),
        samples=[
            TestSample(
                True,
                total_latency_ms=25,
                verification_level="telegram_session",
                dc_id=dc,
                ping_rtts_ms=[20, 21, 22],
                timestamp=time.time() - age,
            )
            for dc in (2, 4, 2)
        ],
        verification_profile="telegram_session",
    )


@pytest.mark.parametrize(
    "link",
    [
        "https://t.me/example_channel/12",
        "https://t.me/Test1/1",
    ],
)
def test_public_link_explicit_post(link):
    channel, message_id = parse_public_message_link(link)
    assert channel and message_id > 0


@pytest.mark.parametrize(
    "link",
    [
        "http://t.me/example/1",
        "https://t.me/c/123/456",
        "https://t.me/+invite",
        "https://t.me/example/1?single",
        "https://t.me/example/1#x",
        "https://t.me/example/0",
        "https://t.me/example",
        "https://t.me:443/example/1",
        "https://evil.test/example/1",
        "https://t.me/../1",
        "https://t.me/login/123",
        "https://user@t.me/example/1",
    ],
)
def test_public_link_rejects_implicit_or_private_targets(link):
    with pytest.raises(ValueError):
        parse_public_message_link(link)


@pytest.mark.parametrize(
    "setting",
    [
        {"max_candidates": 6},
        {"rounds": 1},
        {"timeout_seconds": 601},
        {"budget_bytes": 101 * 1024**2},
        {"round_bytes": CHUNK_BYTES + 4},
        {"proof_max_age_seconds": 1801},
        {"stall_seconds": float("nan")},
    ],
)
def test_hard_media_bounds(setting):
    with pytest.raises(ValueError):
        MediaSettings(**setting)


def test_only_fresh_qualified_unique_candidates():
    good = qualified()
    assert good.strict_eligible
    provisional = qualified("unfinished.example")
    provisional.provisional = True
    expired = qualified("old.example", age=2000)
    selection = select_candidates(
        [expired, provisional, good, good] + [qualified(f"p{i}.example") for i in range(8)]
    )
    assert len(selection) == 5 and selection[0] is good
    with pytest.raises(ValueError):
        select_candidates([expired, provisional])


def test_budget_reservations_prevent_concurrent_overshoot():
    budget = TransferBudget(100)
    budget.reserve(80)
    budget.charge(15)
    with pytest.raises(BudgetExceeded):
        budget.charge(6)
    budget.finish(80, 70)
    assert budget.consumed == 85 and budget.reserved == 0 and budget.exhausted


async def test_partial_read_counts_bytes_and_releases_reservation():
    reader = asyncio.StreamReader()
    reader.feed_data(b"abc")
    reader.feed_eof()
    budget = TransferBudget(100)
    with pytest.raises(asyncio.IncompleteReadError):
        await CountingReader(reader, budget).readexactly(20)
    assert (budget.consumed, budget.reserved) == (3, 0)


async def test_cancelled_read_releases_reserved_bytes():
    budget = TransferBudget(100)
    task = asyncio.create_task(CountingReader(asyncio.StreamReader(), budget).readexactly(20))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.reserved == 0


async def test_cancelled_frame_counts_partial_progress():
    budget = TransferBudget(100)
    reader = asyncio.StreamReader()
    reader.feed_data(b"partial")
    task = asyncio.create_task(CountingReader(reader, budget).readexactly(20))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert budget.reserved == 0 and budget.consumed == 7


class Writer:
    def __init__(self):
        self.data, self.closed = bytearray(), False

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait_closed(self):
        pass


def test_faketls_large_writes_fragment_at_record_limit():
    payload = b"payload" * 10000
    wire = wrap_tls_stream(payload, first=True)
    assert wire.startswith(CHANGE_CIPHER_SPEC)
    offset, decoded, count = 6, bytearray(), 0
    while offset < len(wire):
        assert wire[offset : offset + 3] == b"\x17\x03\x03"
        size = int.from_bytes(wire[offset + 3 : offset + 5], "big")
        assert 0 < size <= TLS_MAX_RECORD
        decoded.extend(wire[offset + 5 : offset + 5 + size])
        offset += 5 + size
        count += 1
    assert decoded == payload and count > 1


async def test_probe_packet_limit_not_relaxed_for_media():
    writer = Writer()
    secret = decode_secret("12" * 16)
    ordinary = _SessionTransport(asyncio.StreamReader(), writer, secret, 2)
    with pytest.raises(ValueError):
        await ordinary.send(b"x" * (MAX_PROBE_RESPONSE + 4))
    media = _SessionTransport(asyncio.StreamReader(), writer, secret, 2, maximum=2 * 1024**2)
    await media.send(b"x" * CHUNK_BYTES)
    assert len(writer.data) > CHUNK_BYTES
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("<I", CHUNK_BYTES) + b"a" * CHUNK_BYTES)
    with pytest.raises(ValueError):
        await _read_frame(reader, padded=True)
    reader = asyncio.StreamReader()
    reader.feed_data(struct.pack("<I", CHUNK_BYTES) + b"a" * CHUNK_BYTES)
    assert len(await _read_frame(reader, True, 2 * 1024**2)) == CHUNK_BYTES


async def test_all_main_media_migration_and_reconnect_sockets_pinned(monkeypatch):
    pytest.importorskip("telethon")
    opened, writers = [], []

    async def opener(host, port):
        opened.append((host, port))
        writer = Writer()
        writers.append(writer)
        return asyncio.StreamReader(), writer

    monkeypatch.setattr(asyncio, "open_connection", opener)
    proxy = Proxy("chosen.example", 8443, "12" * 16)
    connection = connection_factory(proxy, TransferBudget(1000000))
    # Mutation of the caller's original proxy cannot alter this route.
    proxy.server, proxy.port = "wrong.example", 80
    for dc in (2, 4, 5, 2):
        item = connection("149.154.167.50", 443, dc, loggers=defaultdict(lambda: logging.getLogger("test")))
        await item.connect()
        await item.send(b"x" * 40)
        await item.disconnect()
    assert opened == [("chosen.example", 8443)] * 4
    assert all(writer.closed for writer in writers)


async def test_web_route_never_uses_direct_telegram_socket(monkeypatch):
    pytest.importorskip("telethon")
    from telegram_webproxy import carrier
    from telegram_webproxy.models import TelegramWebProxy

    seen = []

    async def open_web(proxy, **kwargs):
        seen.append(proxy.server)
        return asyncio.StreamReader(), Writer()

    monkeypatch.setattr(carrier, "open_connection", open_web)
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=AssertionError("direct socket")))
    factory = connection_factory(TelegramWebProxy("relay.example/path", "12" * 16), TransferBudget(1000))
    connection = factory("dc.example", 443, 4, loggers=defaultdict(lambda: logging.getLogger("test")))
    await connection.connect()
    await connection.disconnect()
    assert seen == ["relay.example/path"]


async def test_client_is_ram_only_cdn_fails_closed_and_login_fetches_no_history():
    pytest.importorskip("telethon")
    from telethon import functions, types
    from telethon.sessions import MemorySession

    from media_test.client import Credentials, make_client

    client = make_client(qualified().proxy, TransferBudget(1000000), Credentials(123, "a" * 32, "+123456789"))
    assert isinstance(client.session, MemorySession)
    assert client._no_updates and not client._catch_up
    client._get_dc = AsyncMock(side_effect=AssertionError("CDN lookup must not run"))
    with pytest.raises(CdnUnsupported):
        await client._get_cdn_client(SimpleNamespace(dc_id=6))
    await client._on_login(types.User(id=123, access_hash=456))
    with pytest.raises(ValueError, match="outside"):
        await client._call(None, functions.updates.GetStateRequest())
    with pytest.raises(ValueError, match="outside"):
        await client._call(
            None,
            functions.messages.GetDialogsRequest(
                offset_date=None, offset_id=0, offset_peer=types.InputPeerEmpty(), limit=1, hash=0
            ),
        )


class Stream:
    def __init__(self, chunks, failure=None):
        self.chunks, self.failure, self.closed = iter(chunks), failure, False

    async def __anext__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.failure:
                raise self.failure
            raise StopAsyncIteration

    async def close(self):
        self.closed = True


async def test_same_prefix_actual_bytes_hash_and_no_file_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    chunks = [b"a" * CHUNK_BYTES, b"b" * CHUNK_BYTES]
    streams, kwargs_seen = [], []

    def download(document, **kwargs):
        kwargs_seen.append(kwargs)
        stream = Stream(chunks)
        streams.append(stream)
        return stream

    client = SimpleNamespace(iter_download=download)
    settings = MediaSettings(round_bytes=2 * CHUNK_BYTES)
    samples = [
        await measure_round(
            client,
            SimpleNamespace(size=2 * CHUNK_BYTES),
            settings,
            TransferBudget(10000000),
            time.perf_counter() + 5,
        )
        for _ in range(2)
    ]
    assert all(item["status"] == "complete" and item["bytes"] == 2 * CHUNK_BYTES for item in samples)
    assert samples[0]["sha256"] == samples[1]["sha256"] == hashlib.sha256(b"".join(chunks)).hexdigest()
    assert all(kwargs["offset"] == 0 and kwargs["request_size"] == CHUNK_BYTES for kwargs in kwargs_seen)
    assert all(stream.closed for stream in streams)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "exception,status",
    [
        (CdnUnsupported(), "cdn_unsupported"),
        (BudgetExceeded(), "budget_limit"),
        (asyncio.CancelledError(), "cancelled"),
        (OSError("private-secret"), "network_failed"),
    ],
)
async def test_partial_download_kept_and_outcomes_not_relabelled_proxy_failure(exception, status):
    stream = Stream([b"a" * CHUNK_BYTES], exception)
    result = await measure_round(
        SimpleNamespace(iter_download=lambda *args, **kwargs: stream),
        SimpleNamespace(size=CHUNK_BYTES * 2),
        MediaSettings(),
        TransferBudget(10000000),
        time.perf_counter() + 5,
    )
    assert result["status"] == status and result["bytes"] == CHUNK_BYTES and stream.closed
    assert "private-secret" not in json.dumps(result)


def test_rpc_account_limit_separate_from_network_failure():
    exception = type("FloodWaitError", (Exception,), {})("private-account")
    assert failure_status(exception, TransferBudget(100)) == "account_limited"


def test_nested_certified_tls_and_dns_reasons():
    outer = RuntimeError("wrapped")
    outer.__cause__ = ssl.SSLCertVerificationError("certificate rejected")
    assert _connection_failure_reason(outer) is FailureReason.TLS_ERROR
    outer.__cause__ = socket.gaierror("resolution failed")
    assert _connection_failure_reason(outer) is FailureReason.DNS_ERROR


def test_report_allowlist_and_atomic_roundtrip(tmp_path):
    doc = {
        "schema_version": 1,
        "status": "complete",
        "phone": "private-phone",
        "api_hash": "secret-api",
        "message_link": "https://t.me/private/1",
        "logout_status": "confirmed",
        "results": [
            {
                "candidate_id": "a" * 64,
                "status": "complete",
                "secret": "private-key",
                "rounds": [{"status": "complete", "bytes": 10, "request": "private-request"}],
            }
        ],
    }
    path = tmp_path / "media-report.json"
    write_report(path, doc)
    payload = path.read_text()
    assert "private" not in payload and "secret-api" not in payload
    assert read_report(path) == safe_report(doc)
    assert not list(tmp_path.glob(".media-*"))
    path.write_text('{"schema_version":1,"results":null}')
    assert read_report(path) is None


async def test_orchestrator_sequential_fresh_clients_logout_and_no_metadata(monkeypatch):
    pytest.importorskip("telethon")
    from telethon.crypto import AuthKey
    from telethon.sessions import MemorySession

    from media_test import runner
    from media_test.client import Credentials

    made, active, maximum = [], set(), 0

    class FakeClient:
        def __init__(self, proxy, budget, account, snapshot=None):
            self.proxy = proxy
            self.session = MemorySession()
            self.session.set_dc(2, "dc.example", 443)
            self.session.auth_key = AuthKey(b"a" * 256 if snapshot is None else snapshot.key)
            self.logged_out, self.snapshot = False, snapshot
            made.append(self)

        async def connect(self):
            nonlocal maximum
            active.add(id(self))
            maximum = max(maximum, len(active))

        async def send_code_request(self, phone):
            assert phone == "+123456789"

        async def sign_in(self, phone=None, code=None, password=None):
            assert code == "12345"

        async def disconnect(self):
            active.discard(id(self))

        async def log_out(self):
            self.logged_out = True
            return True

        def iter_download(self, doc, **kwargs):
            return Stream([b"x" * CHUNK_BYTES])

    monkeypatch.setattr(
        runner, "get_document", AsyncMock(return_value=SimpleNamespace(id=1234, size=CHUNK_BYTES))
    )
    report = await run_media_test_async(
        [qualified(), qualified("other.example")],
        network_profile="test",
        message_link="https://t.me/example/9",
        settings=MediaSettings(round_bytes=CHUNK_BYTES),
        _account=Credentials(123, "a" * 32, "+123456789"),
        _client_factory=FakeClient,
        _prompt=lambda prompt: "12345",
    )
    assert maximum == 1 and not active
    assert report["status"] == "complete" and report["logout_status"] == "confirmed"
    assert len(report["ranked_candidate_ids"]) == 2
    assert len(made) == 4 and made[-1].logged_out
    assert all(client.snapshot is not None for client in made[1:])
    text = json.dumps(report)
    assert "123456789" not in text and "example" not in text and "12345" not in text
    assert all(row["completed_rounds"] == 2 for row in report["results"])
