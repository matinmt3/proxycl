"""Runs MTProto tests across many proxies in parallel using an asyncio semaphore pool."""
from __future__ import annotations

import asyncio
import logging
from typing import List

from config.loader import AppConfig
from tester.mtproto import test_proxy_once
from utils.logger import progress_bar
from utils.models import Proxy, ProxyResult

logger = logging.getLogger("mtselector.tester")


class ParallelTester:
    def __init__(self, config: AppConfig):
        self.config = config

    async def _test_one(self, proxy: Proxy, semaphore: asyncio.Semaphore) -> ProxyResult:
        result = ProxyResult(proxy=proxy)
        async with semaphore:
            for _ in range(max(1, self.config.testing.retries)):
                sample = await test_proxy_once(
                    proxy,
                    connect_timeout=self.config.testing.connect_timeout_seconds,
                    handshake_timeout=self.config.testing.timeout_seconds,
                )
                result.samples.append(sample)
        return result

    async def run_async(self, proxies: List[Proxy]) -> List[ProxyResult]:
        if not proxies:
            return []

        workers = max(1, self.config.testing.workers)
        semaphore = asyncio.Semaphore(workers)
        bar = progress_bar(total=len(proxies), desc=f"Testing ({workers} workers)")

        results: List[ProxyResult] = []

        async def _wrapped(p: Proxy) -> ProxyResult:
            r = await self._test_one(p, semaphore)
            bar.update(1)
            return r

        tasks = [_wrapped(p) for p in proxies]
        results = await asyncio.gather(*tasks)
        bar.close()

        healthy = sum(1 for r in results if r.success_rate > 0)
        logger.info("Tested %d proxies: %d responded successfully at least once", len(results), healthy)
        return list(results)

    def run(self, proxies: List[Proxy]) -> List[ProxyResult]:
        return asyncio.run(self.run_async(proxies))
