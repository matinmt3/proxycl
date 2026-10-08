"""Certificate-verified HTTPS probes over HTTP CONNECT and SOCKS transports."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
import ssl
import struct
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from utils.interruption import ScanInterrupted
from utils.models import FailureReason, ProxyResult, TestSample
from webproxy.models import WebProxy

if TYPE_CHECKING:
    from config.loader import AppConfig

DEFAULT_TARGET = "https://www.gstatic.com/generate_204"
MAX_HEADER_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
logger = logging.getLogger("mtselector.web.tester")


async def _headers(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    raw = await reader.readuntil(b"\r\n\r\n")
    if len(raw) > MAX_HEADER_BYTES:
        raise ValueError("HTTP headers exceed limit")
    lines = raw[:-4].split(b"\r\n")
    match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?", lines[0])
    if match is None:
        raise ValueError("Invalid HTTP status")
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(b":")
        if not separator or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise ValueError("Invalid HTTP header")
        key = name.decode("ascii").lower()
        if key in headers and key in {"content-length", "transfer-encoding"}:
            raise ValueError("Ambiguous HTTP framing")
        if any(byte < 32 and byte != 9 for byte in value) or 127 in value:
            raise ValueError("Invalid HTTP header value")
        headers[key] = value.strip().decode("latin-1")
    return int(match[1]), headers


async def _response(reader: asyncio.StreamReader, expected_status: int) -> None:
    status, headers = await _headers(reader)
    if status != expected_status:
        raise ValueError("Unexpected origin status")
    length = headers.get("content-length")
    transfer = headers.get("transfer-encoding")
    if transfer and length is not None:
        raise ValueError("Ambiguous HTTP response length")
    # RFC 9112 section 6.3: a bodyless response ends at the header boundary,
    # including on a persistent connection. Common Content-Length: 0 remains tolerated.
    if status in {204, 304} and length is None and transfer is None:
        return
    if length is not None:
        if not re.fullmatch(r"[0-9]+", length) or len(length) > 10 or int(length) > MAX_RESPONSE_BYTES:
            raise ValueError("Invalid HTTP response length")
        size = int(length)
        if status in {204, 304} and size:
            raise ValueError("Body forbidden for this status")
        await reader.readexactly(size)
    elif transfer:
        if transfer.lower() != "chunked" or status in {204, 304}:
            raise ValueError("Unsupported HTTP transfer coding")
        total = 0
        while True:
            line = await reader.readuntil(b"\r\n")
            if len(line) > 128 or not re.fullmatch(rb"[0-9A-Fa-f]+", line[:-2]):
                raise ValueError("Invalid HTTP chunk")
            size = int(line[:-2], 16)
            if total + size > MAX_RESPONSE_BYTES:
                raise ValueError("HTTP body exceeds limit")
            if size == 0:
                trailer_bytes = 0
                while True:
                    trailer = await reader.readuntil(b"\r\n")
                    trailer_bytes += len(trailer)
                    if trailer_bytes > MAX_HEADER_BYTES:
                        raise ValueError("HTTP trailers exceed limit")
                    if trailer == b"\r\n":
                        return
                    name, sep, value = trailer[:-2].partition(b":")
                    if not sep or not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                        raise ValueError("Invalid HTTP trailer")
            await reader.readexactly(size)
            if await reader.readexactly(2) != b"\r\n":
                raise ValueError("Invalid HTTP chunk terminator")
            total += size
    else:
        total = 0
        while chunk := await reader.read(min(8192, MAX_RESPONSE_BYTES + 1 - total)):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES or (status in {204, 304} and total):
                raise ValueError("Invalid HTTP body")


async def _tunnel(reader, writer, protocol: str, hostname: str, port: int) -> None:
    encoded_host = hostname.encode("idna")
    if protocol in {"http", "https"}:
        authority = f"[{hostname}]:{port}" if ":" in hostname else f"{hostname}:{port}"
        writer.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii"))
        await writer.drain()
        status, _ = await _headers(reader)
        if not 200 <= status < 300:
            raise ValueError("HTTP proxy rejected CONNECT")
    elif protocol == "socks4":
        if len(encoded_host) > 255 or b"\x00" in encoded_host:
            raise ValueError("SOCKS4a hostname is invalid")
        writer.write(b"\x04\x01" + struct.pack("!H", port) + b"\x00\x00\x00\x01\x00" + encoded_host + b"\x00")
        await writer.drain()
        response = await reader.readexactly(8)
        if response[:2] != b"\x00\x5a":
            raise ValueError("SOCKS4a proxy rejected CONNECT")
    elif protocol == "socks5":
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        if await reader.readexactly(2) != b"\x05\x00":
            raise ValueError("SOCKS5 unauthenticated method refused")
        if not 1 <= len(encoded_host) <= 255:
            raise ValueError("SOCKS5 hostname is invalid")
        writer.write(
            b"\x05\x01\x00\x03" + bytes([len(encoded_host)]) + encoded_host + struct.pack("!H", port)
        )
        await writer.drain()
        response = await reader.readexactly(4)
        if response[:3] != b"\x05\x00\x00":
            raise ValueError("SOCKS5 proxy rejected CONNECT")
        if response[3] == 1:
            await reader.readexactly(6)
        elif response[3] == 4:
            await reader.readexactly(18)
        elif response[3] == 3:
            size = (await reader.readexactly(1))[0]
            if not size:
                raise ValueError("SOCKS5 empty reply hostname")
            await reader.readexactly(size + 2)
        else:
            raise ValueError("SOCKS5 unknown address type")
    else:
        raise ValueError("Unsupported proxy transport")


async def _close(writer) -> None:
    if writer is not None:
        writer.close()
        with contextlib.suppress(OSError, ConnectionError, TimeoutError):
            await asyncio.wait_for(writer.wait_closed(), 0.5)


async def test_proxy_once(
    proxy: WebProxy,
    connect_timeout: float = 3.0,
    handshake_timeout: float = 5.0,
    *,
    target_url: str = DEFAULT_TARGET,
    expected_status: int = 204,
    ssl_context: ssl.SSLContext | None = None,
) -> TestSample:
    """Success requires a complete HTTPS response through this exact proxy."""
    started = time.perf_counter()
    writer = None
    tcp_latency = None
    connecting = True
    try:
        target = urlsplit(target_url)
        if (
            target.scheme != "https"
            or not target.hostname
            or target.username
            or target.password
            or target.fragment
        ):
            raise ValueError("Probe target must be an unauthenticated HTTPS URL")
        hostname = target.hostname.encode("idna").decode("ascii")
        port = target.port or 443
        context = ssl_context if ssl_context is not None else ssl.create_default_context()
        if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
            raise ValueError("Probe requires hostname and certificate validation")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                proxy.server,
                proxy.port,
                limit=MAX_HEADER_BYTES,
                happy_eyeballs_delay=0.1,
                ssl=context if proxy.protocol == "https" else None,
                server_hostname=proxy.server if proxy.protocol == "https" else None,
                ssl_handshake_timeout=connect_timeout if proxy.protocol == "https" else None,
            ),
            connect_timeout,
        )
        tcp_latency = (time.perf_counter() - started) * 1000
        connecting = False
        async with asyncio.timeout(handshake_timeout):
            await _tunnel(reader, writer, proxy.protocol, hostname, port)
            await writer.start_tls(context, server_hostname=hostname, ssl_handshake_timeout=handshake_timeout)
            path = target.path or "/"
            if target.query:
                path += "?" + target.query
            if any(ord(c) < 33 or ord(c) > 126 for c in path):
                raise ValueError("Probe target path must be URL encoded")
            authority = f"[{hostname}]" if ":" in hostname else hostname
            if port != 443:
                authority += f":{port}"
            writer.write(
                f"GET {path} HTTP/1.1\r\nHost: {authority}\r\nUser-Agent: Revmamad/3.0\r\nAccept: */*\r\nConnection: close\r\n\r\n".encode(
                    "ascii"
                )
            )
            await writer.drain()
            await _response(reader, expected_status)
        total = (time.perf_counter() - started) * 1000
        return TestSample(
            True, tcp_latency_ms=tcp_latency, handshake_latency_ms=total - tcp_latency, total_latency_ms=total
        )
    except TimeoutError:
        reason = FailureReason.TCP_TIMEOUT if connecting else FailureReason.HANDSHAKE_TIMEOUT
    except socket.gaierror:
        reason = FailureReason.DNS_ERROR
    except ConnectionRefusedError:
        reason = FailureReason.TCP_REFUSED
    except (ValueError, ssl.SSLError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
        reason = FailureReason.HANDSHAKE_INVALID
    finally:
        await _close(writer)
    return TestSample(False, tcp_latency_ms=tcp_latency, failure_reason=reason)


test_proxy_once.__test__ = False


class WebTester:
    def __init__(
        self,
        config: AppConfig,
        *,
        target_url: str = DEFAULT_TARGET,
        expected_status: int = 204,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self.config = config
        self.target_url = target_url
        self.expected_status = expected_status
        self.ssl_context = ssl_context
        self.partial_results: list[ProxyResult] = []

    async def _test_one(self, proxy: WebProxy, result: ProxyResult | None = None) -> ProxyResult:
        if result is None:
            result = ProxyResult(proxy)
        settings = self.config.testing
        for _ in range(max(1, settings.retries)):
            sample = await test_proxy_once(
                proxy,
                settings.connect_timeout_seconds,
                settings.timeout_seconds,
                target_url=self.target_url,
                expected_status=self.expected_status,
                ssl_context=self.ssl_context,
            )
            result.samples.append(sample)
        return result

    async def run_async(self, proxies: list[WebProxy]) -> list[ProxyResult]:
        self.partial_results = []
        if not proxies:
            return []
        from tester.runner import worker_count
        from utils.logger import progress_bar

        workers = worker_count(self.config.testing.workers, len(proxies))
        bar = progress_bar(total=len(proxies), desc=f"Web HTTPS probe ({workers} workers)")
        results: list[ProxyResult | None] = [None] * len(proxies)
        pending = iter(enumerate(proxies))

        async def worker() -> None:
            for index, proxy in pending:
                result = results[index] = ProxyResult(proxy)
                await self._test_one(proxy, result)
                bar.update(1)

        tasks = [asyncio.create_task(worker()) for _ in range(workers)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            bar.close()
            self.partial_results = [
                ProxyResult(result.proxy, list(result.samples), result.score)
                for result in results
                if result is not None and result.samples
            ]
        return [result for result in results if result is not None]

    def run(self, proxies: list[WebProxy]) -> list[ProxyResult]:
        self.partial_results = []
        try:
            return asyncio.run(self.run_async(proxies))
        except (KeyboardInterrupt, asyncio.CancelledError) as interrupted:
            raise ScanInterrupted(self.partial_results) from interrupted
