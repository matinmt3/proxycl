"""Run probes with a bounded worker pool and predictable cancellation."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from dataclasses import replace

from config.loader import AppConfig
from tester.mtproto import test_proxy_once
from utils.interruption import ScanInterrupted
from utils.logger import progress_bar
from utils.models import Proxy, ProxyResult, TelegramQualityPolicy
from utils.scan_control import AdaptiveLimiter, ScanController, ScanStopped, scan_deadline

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


def mtproto_worker_count(settings, candidates: int) -> int:
    return worker_count(min(settings.workers, settings.mtproto_workers), candidates)


class ParallelTester:
    def __init__(self, config: AppConfig, *, controller: ScanController | None = None):
        self.config = config
        self.partial_results: list[ProxyResult] = []
        self.initial_results: list[ProxyResult] = []
        self.on_sample = None
        self._current_results: list[ProxyResult | None] = []
        self.controller = controller or getattr(config, "_scan_controller", None) or ScanController()
        self._limiter = None
        self._budget_exhausted = False

    @property
    def summary(self) -> dict:
        return {
            "budget_exhausted": self._budget_exhausted,
            **(self._limiter.summary if self._limiter is not None else {}),
        }

    def _new_result(self, proxy: Proxy) -> ProxyResult:
        settings = self.config.testing
        if settings.mtproto_profile == "transport":
            return ProxyResult(proxy)
        output = self.config.output
        return ProxyResult(
            proxy,
            verification_profile="telegram_session",
            quality_policy=TelegramQualityPolicy(
                min_successes=output.mtproto_min_successes,
                min_success_rate=output.mtproto_min_success_rate,
                required_dc_ids=tuple(settings.telegram_dc_ids),
                min_ping_count=settings.ping_count,
                max_p95_ms=output.mtproto_max_ping_p95_ms,
                max_jitter_ms=output.mtproto_max_ping_jitter_ms,
            ),
        )

    async def _test_one(self, proxy: Proxy, result: ProxyResult | None = None) -> ProxyResult:
        if result is None:
            result = self._new_result(proxy)
        settings = self.config.testing
        for attempt in range(len(result.samples), max(1, settings.retries)):
            self.controller.check()
            async with self._limiter.slot() if self._limiter is not None else contextlib.nullcontext():
                self.controller.check()
                if settings.mtproto_profile == "telegram_session":
                    from tester.mtproto import test_proxy_strict

                    sample = await test_proxy_strict(
                        proxy,
                        connect_timeout=settings.connect_timeout_seconds,
                        handshake_timeout=settings.auth_timeout_seconds,
                        dc_id=settings.telegram_dc_ids[attempt % len(settings.telegram_dc_ids)],
                        ping_count=settings.ping_count,
                        ping_interval=settings.ping_interval_seconds,
                        ping_timeout=settings.ping_timeout_seconds,
                    )
                else:
                    sample = await test_proxy_once(
                        proxy,
                        connect_timeout=settings.connect_timeout_seconds,
                        handshake_timeout=settings.timeout_seconds,
                    )
            result.samples.append(sample)
            if self.on_sample is not None:
                notification = self.on_sample(result)
                if inspect.isawaitable(notification):
                    await notification
        return result

    async def run_async(self, proxies: list[Proxy]) -> list[ProxyResult]:
        self.partial_results = []
        self._budget_exhausted = False
        if not proxies:
            return []
        workers = mtproto_worker_count(self.config.testing, len(proxies))
        scan = getattr(self.config, "scan", None)
        self._limiter = AdaptiveLimiter(workers, enabled=getattr(scan, "adaptive_workers", True))
        deadline = scan_deadline(self.config)
        bar = progress_bar(total=len(proxies), desc=f"Testing ({workers} workers)")
        initial = {result.proxy.key(): result for result in self.initial_results}
        results: list[ProxyResult | None] = [initial.get(proxy.key()) for proxy in proxies]
        self._current_results = results
        pending = iter(enumerate(proxies))

        async def worker() -> None:
            # next() occurs before await: each worker takes a distinct candidate.
            for index, proxy in pending:
                result = results[index] = results[index] or self._new_result(proxy)
                await self._test_one(proxy, result)
                bar.update(1)

        async def run_workers():
            async with self._limiter:
                tasks = [asyncio.create_task(worker()) for _ in range(workers)]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await self.controller.run(run_workers, deadline=deadline)
        except ScanStopped as exc:
            self._budget_exhausted = exc.reason == "budget"
            raise
        finally:
            bar.close()
            self.partial_results = [
                replace(result, samples=list(result.samples))
                for result in results
                if result is not None and result.samples
            ]
        completed = [result for result in results if result is not None]
        healthy = sum(result.success_rate > 0 for result in completed)
        logger.info("Tested %d proxies: %d returned a valid MTProto response", len(completed), healthy)
        return completed

    def run(self, proxies: list[Proxy]) -> list[ProxyResult]:
        self.partial_results = []
        try:
            return asyncio.run(self.run_async(proxies))
        except (KeyboardInterrupt, asyncio.CancelledError) as interrupted:
            raise ScanInterrupted(self.partial_results) from interrupted
