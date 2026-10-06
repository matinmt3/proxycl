"""Run probes with a bounded worker pool and predictable cancellation."""

from __future__ import annotations

import asyncio
import logging
import os

from config.loader import AppConfig
from tester.mtproto import test_proxy_once
from utils.logger import progress_bar
from utils.models import Proxy, ProxyResult

logger = logging.getLogger("mtselector.tester")


def worker_count(requested: int, candidates: int) -> int:
    workers = min(max(1, requested), candidates, 1000)
    if os.environ.get("TERMUX_VERSION") or "/com.termux/" in os.environ.get("PREFIX", ""):
        workers = min(workers, 64)
    try:
        import resource

        soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft_limit != resource.RLIM_INFINITY:
            workers = min(workers, max(1, (soft_limit - 64) // 2))
    except (ImportError, ValueError, OSError):
        pass
    return workers


class ParallelTester:
    def __init__(self, config: AppConfig):
        self.config = config

    async def _test_one(self, proxy: Proxy) -> ProxyResult:
        result = ProxyResult(proxy=proxy)
        for _ in range(max(1, self.config.testing.retries)):
            sample = await test_proxy_once(
                proxy,
                connect_timeout=self.config.testing.connect_timeout_seconds,
                handshake_timeout=self.config.testing.timeout_seconds,
            )
            result.samples.append(sample)
        return result

    async def run_async(self, proxies: list[Proxy]) -> list[ProxyResult]:
        if not proxies:
            return []
        workers = worker_count(self.config.testing.workers, len(proxies))
        bar = progress_bar(total=len(proxies), desc=f"Testing ({workers} workers)")
        results: list[ProxyResult | None] = [None] * len(proxies)
        pending = iter(enumerate(proxies))

        async def worker() -> None:
            # next() occurs before await: each worker takes a distinct candidate.
            for index, proxy in pending:
                results[index] = await self._test_one(proxy)
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
        completed = [result for result in results if result is not None]
        healthy = sum(result.success_rate > 0 for result in completed)
        logger.info("Tested %d proxies: %d returned a valid MTProto response", len(completed), healthy)
        return completed

    def run(self, proxies: list[Proxy]) -> list[ProxyResult]:
        return asyncio.run(self.run_async(proxies))
