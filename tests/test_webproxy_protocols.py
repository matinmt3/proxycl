import asyncio
import contextlib
import ssl
import struct
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from tests.test_webproxy_tls import CERTIFICATE, PRIVATE_KEY


@pytest.fixture
def tls_contexts(tmp_path):
    cert = tmp_path / "cert.pem"
    key = tmp_path / "key.pem"
    cert.write_text(CERTIFICATE)
    key.write_text(PRIVATE_KEY)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(cert, key)
    client = ssl.create_default_context(cadata=CERTIFICATE)
    return server, client


@asynccontextmanager
async def protocol_fixture(
    kind,
    tls_contexts,
    *,
    status=204,
    response=None,
    proxy_reply=None,
    fragment=False,
    stall=False,
    delay=0,
    statuses=None,
    target_host="localhost",
    connect_status=200,
    keep_alive=False,
):
    """Independent relay speaks literal protocol packets and a TLS HTTPS origin."""
    server_context, client_context = tls_contexts
    requests, protocol_requests = [], []
    activity = {"active": 0, "maximum": 0}
    clients, tasks = set(), set()

    async def close(writer):
        writer.close()
        with contextlib.suppress(ConnectionError, OSError):
            await writer.wait_closed()

    async def origin(reader, writer):
        clients.add(writer)
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            requests.append(request)
            activity["active"] += 1
            activity["maximum"] = max(activity["maximum"], activity["active"])
            if delay:
                await asyncio.sleep(delay)
            if stall:
                await reader.read()
            else:
                chosen_status = statuses[(len(requests) - 1) % len(statuses)] if statuses else status
                body = (
                    response
                    if response is not None
                    else f"HTTP/1.1 {chosen_status} Test\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
                )
                if fragment:
                    for byte in body:
                        writer.write(bytes([byte]))
                        await writer.drain()
                        await asyncio.sleep(0)
                else:
                    writer.write(body)
                    await writer.drain()
                if keep_alive:
                    await reader.read()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            activity["active"] -= 1
            await close(writer)
            clients.discard(writer)

    origin_server = await asyncio.start_server(origin, "127.0.0.1", 0, ssl=server_context)
    origin_port = origin_server.sockets[0].getsockname()[1]

    async def relay(reader, writer):
        clients.add(writer)
        upstream = None
        try:
            if kind in ("http", "https"):
                request = await reader.readuntil(b"\r\n\r\n")
                protocol_requests.append(request)
                assert (
                    request.split(b"\r\n", 1)[0] == f"CONNECT {target_host}:{origin_port} HTTP/1.1".encode()
                )
                reply = (
                    proxy_reply
                    if proxy_reply is not None
                    else f"HTTP/1.1 {connect_status} Connection established\r\n\r\n".encode()
                )
            elif kind == "socks4":
                request = await reader.readexactly(8)
                user = await reader.readuntil(b"\x00")
                hostname = await reader.readuntil(b"\x00")
                protocol_requests.append(request + user + hostname)
                assert request == b"\x04\x01" + struct.pack("!H", origin_port) + b"\x00\x00\x00\x01"
                assert user == b"\x00" and hostname == b"localhost\x00"
                reply = proxy_reply if proxy_reply is not None else b"\x00\x5a\x00\x00\x00\x00\x00\x00"
            else:
                greeting = await reader.readexactly(3)
                assert greeting == b"\x05\x01\x00"
                writer.write(b"\x05\x00")
                await writer.drain()
                header = await reader.readexactly(5)
                assert header == b"\x05\x01\x00\x03\x09"
                rest = await reader.readexactly(11)
                assert rest == b"localhost" + struct.pack("!H", origin_port)
                protocol_requests.append(header + rest)
                reply = (
                    proxy_reply if proxy_reply is not None else b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00"
                )
            if fragment:
                for byte in reply:
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(0)
            else:
                writer.write(reply)
                await writer.drain()
            if proxy_reply is not None:
                return
            upstream_reader, upstream = await asyncio.open_connection("127.0.0.1", origin_port)
            clients.add(upstream)

            async def pump(source, target):
                while data := await source.read(8192):
                    target.write(data)
                    await target.drain()

            pumps = [
                asyncio.create_task(pump(reader, upstream)),
                asyncio.create_task(pump(upstream_reader, writer)),
            ]
            tasks.update(pumps)
            done, pending = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
            tasks.difference_update(pumps)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if upstream:
                await close(upstream)
                clients.discard(upstream)
            await close(writer)
            clients.discard(writer)

    proxy_server = await asyncio.start_server(
        relay, "127.0.0.1", 0, ssl=server_context if kind == "https" else None
    )
    proxy_port = proxy_server.sockets[0].getsockname()[1]
    try:
        yield {
            "proxy_port": proxy_port,
            "target": f"https://{target_host}:{origin_port}/generate_204",
            "client_context": client_context,
            "requests": requests,
            "protocol_requests": protocol_requests,
            "clients": clients,
            "activity": activity,
        }
    finally:
        proxy_server.close()
        origin_server.close()
        await proxy_server.wait_closed()
        await origin_server.wait_closed()
        for writer in list(clients):
            await close(writer)
        for task in list(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "https", "socks4", "socks5"])
async def test_complete_certificate_verified_https_round_trip_over_each_proxy(kind, tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    async with protocol_fixture(kind, tls_contexts, fragment=True) as local:
        sample = await probe(
            WebProxy("localhost", local["proxy_port"], kind),
            target_url=local["target"],
            ssl_context=local["client_context"],
        )
        assert sample.success is True
        assert sample.total_latency_ms >= sample.tcp_latency_ms >= 0
        assert len(local["protocol_requests"]) == 1
        assert len(local["requests"]) == 1
        assert local["requests"][0].startswith(b"GET /generate_204 HTTP/1.1\r\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http", "https"])
async def test_untrusted_origin_or_proxy_certificate_is_rejected(kind, tls_contexts):
    from utils.models import FailureReason
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    async with protocol_fixture(kind, tls_contexts) as local:
        sample = await probe(WebProxy("localhost", local["proxy_port"], kind), target_url=local["target"])
        assert sample.success is False
        assert sample.failure_reason == FailureReason.HANDSHAKE_INVALID
        assert local["requests"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n",
        b"not-http\r\n\r\n",
        b"HTTP/1.1 204 OK\r\nBad header\r\n\r\n",
        b"HTTP/1.1 204 OK\r\nContent-Length: 99999999\r\n\r\n",
        b"HTTP/1.1 204 OK\r\nContent-Length: 2\r\n\r\nx",
        b"HTTP/1.1 204 OK\r\nX: " + b"a" * 20000 + b"\r\n\r\n",
    ],
)
async def test_bad_status_malformed_truncated_or_oversized_origin_is_not_verified(response, tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    async with protocol_fixture("http", tls_contexts, response=response) as local:
        sample = await probe(
            WebProxy("localhost", local["proxy_port"], "http"),
            target_url=local["target"],
            ssl_context=local["client_context"],
        )
        assert sample.success is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,reply",
    [
        ("http", b"HTTP/1.1 407 Authentication Required\r\n\r\n"),
        ("socks4", b"\x00\x5b\x00\x00\x00\x00\x00\x00"),
        ("socks5", b"\x05\x05\x00\x01\x7f\x00\x00\x01\x00\x00"),
    ],
)
async def test_proxy_rejection_cannot_fall_back_to_direct_origin(kind, reply, tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    async with protocol_fixture(kind, tls_contexts, proxy_reply=reply) as local:
        sample = await probe(
            WebProxy("localhost", local["proxy_port"], kind),
            target_url=local["target"],
            ssl_context=local["client_context"],
        )
        assert sample.success is False
        assert local["requests"] == []


@pytest.mark.asyncio
async def test_stalled_origin_is_bounded_by_complete_handshake_timeout(tls_contexts):
    from utils.models import FailureReason
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    async with protocol_fixture("http", tls_contexts, stall=True) as local:
        sample = await asyncio.wait_for(
            probe(
                WebProxy("localhost", local["proxy_port"]),
                target_url=local["target"],
                ssl_context=local["client_context"],
                handshake_timeout=0.1,
            ),
            1,
        )
        assert not sample.success
        assert sample.failure_reason == FailureReason.HANDSHAKE_TIMEOUT


@pytest.mark.asyncio
async def test_cancelling_probe_closes_proxy_connection(tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    async with protocol_fixture("http", tls_contexts, stall=True) as local:
        task = asyncio.create_task(
            probe(
                WebProxy("localhost", local["proxy_port"]),
                target_url=local["target"],
                ssl_context=local["client_context"],
            )
        )
        for _ in range(100):
            if local["requests"]:
                break
            await asyncio.sleep(0.01)
        assert local["requests"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not local["clients"]:
                break
            await asyncio.sleep(0.01)
        assert not local["clients"]


def make_testing_config(workers=2, retries=1):
    return SimpleNamespace(
        testing=SimpleNamespace(
            workers=workers, retries=retries, connect_timeout_seconds=1, timeout_seconds=2
        )
    )


@pytest.mark.asyncio
async def test_worker_pool_limits_real_concurrent_round_trips_and_preserves_result_order(tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import WebTester

    async with protocol_fixture("http", tls_contexts, delay=0.05) as local:
        candidates = [WebProxy("localhost", local["proxy_port"], source=str(i)) for i in range(6)]
        tester = WebTester(
            make_testing_config(workers=2), target_url=local["target"], ssl_context=local["client_context"]
        )
        results = await tester.run_async(candidates)
        assert [r.proxy.source for r in results] == ["0", "1", "2", "3", "4", "5"]
        assert all(r.successes == 1 and r.attempts == 1 for r in results)
        assert local["activity"]["maximum"] == 2
        assert len(local["requests"]) == 6


@pytest.mark.asyncio
async def test_retries_include_failed_and_successful_full_https_samples(tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import WebTester

    async with protocol_fixture("http", tls_contexts, statuses=[503, 204, 204]) as local:
        tester = WebTester(
            make_testing_config(retries=3), target_url=local["target"], ssl_context=local["client_context"]
        )
        results = await tester.run_async([WebProxy("localhost", local["proxy_port"])])
        assert len(local["requests"]) == 3
        assert results[0].attempts == 3 and results[0].successes == 2
        assert results[0].success_rate == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_cancelled_worker_pool_closes_live_connections_without_starting_new_candidates(tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import WebTester

    async with protocol_fixture("http", tls_contexts, stall=True) as local:
        tester = WebTester(
            make_testing_config(workers=2, retries=3),
            target_url=local["target"],
            ssl_context=local["client_context"],
        )
        task = asyncio.create_task(
            tester.run_async([WebProxy("localhost", local["proxy_port"]) for _ in range(40)])
        )
        for _ in range(100):
            if len(local["requests"]) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(local["requests"]) == 2
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        for _ in range(100):
            if not local["clients"]:
                break
            await asyncio.sleep(0.01)
        assert len(local["requests"]) == 2
        assert not local["clients"]


@pytest.mark.asyncio
async def test_trusted_certificate_for_wrong_origin_hostname_is_rejected(tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    async with protocol_fixture("http", tls_contexts, target_host="127.0.0.1") as local:
        sample = await probe(
            WebProxy("localhost", local["proxy_port"]),
            target_url=local["target"],
            ssl_context=local["client_context"],
        )
        assert not sample.success
        assert not local["requests"]


@pytest.mark.asyncio
async def test_successful_non200_connect_status_still_tunnels_verified_https(tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    async with protocol_fixture("http", tls_contexts, connect_status=201) as local:
        sample = await probe(
            WebProxy("localhost", local["proxy_port"]),
            target_url=local["target"],
            ssl_context=local["client_context"],
        )
        assert sample.success is True
        assert len(local["requests"]) == 1


@pytest.mark.asyncio
async def test_204_without_content_length_completes_before_persistent_origin_closes(tls_contexts):
    from webproxy.models import WebProxy
    from webproxy.tester import test_proxy_once as probe

    response = b"HTTP/1.1 204 No Content\r\nConnection: keep-alive\r\n\r\n"
    async with protocol_fixture("http", tls_contexts, response=response, keep_alive=True) as local:
        sample = await probe(
            WebProxy("localhost", local["proxy_port"]),
            target_url=local["target"],
            ssl_context=local["client_context"],
            handshake_timeout=0.2,
        )
        assert sample.success is True
        assert len(local["requests"]) == 1
