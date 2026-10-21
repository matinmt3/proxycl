"""Bounded transport screening, Telegram sessions and sustained finalist tests.

The prefilter is only a cost-saving diagnostic. Recommendations are earned by
fresh encrypted Telegram sessions; completed scans require sustained finalist
evidence even when a short baseline happened to be fast. Checkpoints contain
only completed samples and can resume between any two connection attempts.
"""

from __future__ import annotations

import asyncio
import inspect
import math
from dataclasses import replace

from benchmark.scorer import rank, score_all
from config.loader import AppConfig
from tester.mtproto import test_proxy_once, test_proxy_strict
from tester.runner import ParallelTester, mtproto_worker_count, worker_count
from utils.interruption import ScanInterrupted
from utils.models import FailureReason, Proxy, ProxyResult
from utils.scan_state import deserialize_result, serialize_result


class StagedTester:
    """Run independently resumable stages, with no unbounded candidate tasks."""

    def __init__(
        self,
        config: AppConfig,
        *,
        initial_state: dict | None = None,
        on_checkpoint=None,
        on_progress=None,
    ):
        self.config = config
        self.initial_state = initial_state
        self.on_checkpoint = on_checkpoint
        self.on_progress = on_progress
        self.partial_results: list[ProxyResult] = []
        self._stage = "prefilter" if config.scan.prefilter else "baseline"
        self._proxies: list[Proxy] = []
        self._results: dict[str, ProxyResult] = {}
        self._wire_results: dict[str, dict] = {}
        self._prefilter: dict[str, dict] = {}
        self._finalist_keys: list[str] = []
        self._finalist_attempts: dict[str, int] = {}
        self._prefilter_passed = 0
        self._baseline_completed: set[str] = set()
        self._baseline_qualified: dict[str, bool] = {}
        self._qualified: dict[str, bool] = {}
        self._strict_attempts_completed = 0
        self._callback_lock = None

    def _baseline_result(self, result: ProxyResult) -> ProxyResult:
        baseline = ParallelTester(self.config)._new_result(result.proxy)
        baseline.samples = list(result.samples[: self.config.testing.retries])
        return baseline

    def _new_result(self, proxy: Proxy) -> ProxyResult:
        return ParallelTester(self.config)._new_result(proxy)

    def _completion_minimum(self) -> int:
        """Baseline-only results must not bypass the sustained finalist stage."""
        total = self.config.testing.retries + self.config.scan.finalist_retries
        return max(
            self.config.output.mtproto_min_successes,
            self.config.testing.retries + 1,
            math.ceil(self.config.output.mtproto_min_success_rate * total),
        )

    def _set_policies(self, *, completed: bool) -> None:
        for key, result in self._results.items():
            policy = self._new_result(result.proxy).quality_policy
            if completed:
                policy = replace(policy, min_successes=self._completion_minimum())
            result.quality_policy = policy
            self._wire_results[key] = serialize_result(result)
            self._qualified[key] = result.strict_eligible

    @property
    def state(self) -> dict:
        """Return a replacement-only snapshot safe for an awaited disk callback.

        Stored row objects are never mutated after publication: a later finished
        sample replaces its entire row. Callers need not deep-copy a snapshot
        before handing it to their bounded atomic checkpoint writer.
        """
        return {
            "schema": 1,
            "stage": self._stage,
            "prefilter": dict(self._prefilter),
            "results": [self._wire_results[p.key()] for p in self._proxies if p.key() in self._wire_results],
            "finalist_keys": list(self._finalist_keys),
            "finalist_attempts": dict(self._finalist_attempts),
        }

    @property
    def summary(self) -> dict:
        passed = self._prefilter_passed
        baseline_tested = len(self._baseline_completed)
        baseline_qualified = sum(self._baseline_qualified.values())
        finalists_tested = sum(
            attempts == self.config.scan.finalist_retries for attempts in self._finalist_attempts.values()
        )
        qualified = sum(self._qualified.values())
        if self._stage == "prefilter":
            completed, total = len(self._prefilter), len(self._proxies)
        elif self._stage == "baseline":
            completed, total = baseline_tested, passed if self.config.scan.prefilter else len(self._proxies)
        else:
            completed, total = finalists_tested, len(self._finalist_keys)
        return {
            "stage": self._stage,
            "provisional": self._stage != "complete",
            "prefilter_tested": len(self._prefilter),
            "prefilter_passed": passed,
            "prefilter_failed": len(self._prefilter) - passed,
            "baseline_tested": baseline_tested,
            "baseline_qualified": baseline_qualified,
            "finalists_selected": len(self._finalist_keys),
            "finalists_tested": finalists_tested,
            "finalist_attempts_completed": sum(self._finalist_attempts.values()),
            "strict_attempts_completed": self._strict_attempts_completed,
            "completed": completed,
            "total": total,
            "qualified": qualified,
        }

    async def _notify(self) -> None:
        async with self._callback_lock:
            if self.on_checkpoint is not None:
                response = self.on_checkpoint(self.state)
                if inspect.isawaitable(response):
                    await response
            if self.on_progress is not None:
                response = self.on_progress(self.summary)
                if inspect.isawaitable(response):
                    await response

    def _restore(self, proxies: list[Proxy]) -> None:
        self._proxies = list(dict((proxy.key(), proxy) for proxy in proxies).values())
        self._results = {}
        self._wire_results = {}
        self._prefilter = {}
        self._finalist_keys = []
        self._finalist_attempts = {}
        self._prefilter_passed = 0
        self._baseline_completed = set()
        self._baseline_qualified = {}
        self._qualified = {}
        self._strict_attempts_completed = 0
        self._stage = "prefilter" if self.config.scan.prefilter else "baseline"
        row = self.initial_state
        if row is None:
            return
        if not isinstance(row, dict) or type(row.get("schema")) is not int or row["schema"] != 1:
            raise ValueError("Invalid staged checkpoint schema")
        stage = row.get("stage")
        if not isinstance(stage, str) or stage not in {"prefilter", "baseline", "finalists", "complete"}:
            raise ValueError("Invalid staged checkpoint stage")
        candidates = {proxy.key(): proxy for proxy in self._proxies}
        prefilter = row.get("prefilter")
        if not isinstance(prefilter, dict) or set(prefilter) - candidates.keys():
            raise ValueError("Invalid staged checkpoint prefilter candidates")
        for key, outcome in prefilter.items():
            if not isinstance(outcome, dict) or type(outcome.get("passed")) is not bool:
                raise ValueError("Invalid staged checkpoint prefilter outcome")
            try:
                reason = FailureReason(outcome.get("reason"))
            except (ValueError, TypeError) as exc:
                raise ValueError("Invalid staged checkpoint prefilter reason") from exc
            if outcome["passed"] and reason != FailureReason.NONE:
                raise ValueError("Successful prefilter cannot contain a failure reason")
            self._prefilter[key] = {"passed": outcome["passed"], "reason": reason.value}
            self._prefilter_passed += outcome["passed"]
        if not self.config.scan.prefilter and (prefilter or stage == "prefilter"):
            raise ValueError("Checkpoint prefilter does not match scan settings")
        if self.config.scan.prefilter and stage != "prefilter" and set(prefilter) != candidates.keys():
            raise ValueError("Checkpoint advanced before the prefilter completed")
        rows = row.get("results")
        if not isinstance(rows, list) or len(rows) > len(candidates):
            raise ValueError("Invalid staged checkpoint results")
        for saved in rows:
            result = deserialize_result(saved)
            key = result.proxy.key()
            if (
                not isinstance(result.proxy, Proxy)
                or result.verification_profile != "telegram_session"
                or key not in candidates
                or key in self._results
                or not result.samples
            ):
                raise ValueError("Invalid staged checkpoint result candidate")
            if self.config.scan.prefilter and not self._prefilter.get(key, {}).get("passed"):
                raise ValueError("Strict checkpoint result did not pass the prefilter")
            result.proxy = candidates[key]
            # Derived policy is always reconstructed from current validated config.
            result.quality_policy = self._new_result(result.proxy).quality_policy
            self._results[key] = result
            self._wire_results[key] = serialize_result(result)
            self._baseline_qualified[key] = self._baseline_result(result).strict_eligible
            self._qualified[key] = result.strict_eligible
            self._strict_attempts_completed += result.attempts
            if result.attempts >= self.config.testing.retries:
                self._baseline_completed.add(key)
        keys, attempts = row.get("finalist_keys"), row.get("finalist_attempts")
        if (
            not isinstance(keys, list)
            or any(not isinstance(key, str) for key in keys)
            or len(set(keys)) != len(keys)
            or len(keys) > self.config.scan.finalists
            or set(keys) - self._results.keys()
            or not isinstance(attempts, dict)
            or set(attempts) != set(keys)
        ):
            raise ValueError("Invalid staged checkpoint finalist selection")
        self._finalist_keys = list(keys)
        budget = self.config.scan.finalist_retries
        for key in keys:
            value = attempts[key]
            if type(value) is not int or not 0 <= value <= budget:
                raise ValueError("Invalid staged checkpoint finalist attempt count")
            if not self._baseline_result(self._results[key]).strict_eligible:
                raise ValueError("Checkpoint finalist did not qualify in the baseline")
            self._finalist_attempts[key] = value
        baseline_budget = self.config.testing.retries
        if stage == "prefilter" and self._results:
            raise ValueError("Checkpoint strict results precede the baseline stage")
        if stage in {"prefilter", "baseline"} and (keys or attempts):
            raise ValueError("Checkpoint finalists precede completion of the baseline")
        for key, result in self._results.items():
            expected = baseline_budget + self._finalist_attempts.get(key, 0)
            if result.attempts > expected or (
                stage in {"finalists", "complete"} and result.attempts != expected
            ):
                raise ValueError("Checkpoint sample count does not match completed stages")
        if stage in {"finalists", "complete"}:
            expected_candidates = {
                key for key in candidates if not self.config.scan.prefilter or self._prefilter[key]["passed"]
            }
            if self._results.keys() != expected_candidates:
                raise ValueError("Checkpoint advanced before all baseline candidates finished")
        if stage == "complete" and any(value != budget for value in self._finalist_attempts.values()):
            raise ValueError("Checkpoint completed before all finalist attempts finished")
        self._stage = stage
        self._set_policies(completed=stage == "complete")

    async def _pool(self, proxies: list[Proxy], workers: int, operation) -> None:
        if not proxies:
            return
        pending = iter(proxies)

        async def worker():
            for proxy in pending:
                await operation(proxy)

        tasks = [asyncio.create_task(worker()) for _ in range(min(workers, len(proxies)))]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run_prefilter(self) -> None:
        settings = self.config.testing

        async def test(proxy):
            sample = await test_proxy_once(
                proxy,
                connect_timeout=settings.connect_timeout_seconds,
                handshake_timeout=settings.timeout_seconds,
            )
            self._prefilter[proxy.key()] = {
                "passed": sample.success,
                "reason": sample.failure_reason.value,
            }
            self._prefilter_passed += sample.success
            await self._notify()

        pending = [proxy for proxy in self._proxies if proxy.key() not in self._prefilter]
        await self._pool(pending, mtproto_worker_count(settings, len(pending)), test)
        self._stage = "baseline"
        await self._notify()

    async def _strict_attempt(self, proxy: Proxy, *, finalist: bool) -> None:
        settings, scan = self.config.testing, self.config.scan
        key = proxy.key()
        result = self._results.get(key)
        if result is None:
            result = self._new_result(proxy)
        dc = settings.telegram_dc_ids[result.attempts % len(settings.telegram_dc_ids)]
        sample = await test_proxy_strict(
            proxy,
            connect_timeout=settings.connect_timeout_seconds,
            handshake_timeout=settings.auth_timeout_seconds,
            dc_id=dc,
            ping_count=(
                max(scan.finalist_ping_count, settings.ping_count) if finalist else settings.ping_count
            ),
            ping_interval=scan.finalist_ping_interval_seconds if finalist else settings.ping_interval_seconds,
            ping_timeout=settings.ping_timeout_seconds,
        )
        # Append and publish synchronously before awaiting the callback lock. A
        # cancellation while another worker checkpoints cannot lose this sample.
        result.samples.append(sample)
        self._results[key] = result
        self._strict_attempts_completed += 1
        if finalist:
            self._finalist_attempts[key] += 1
        else:
            self._baseline_qualified[key] = result.strict_eligible
            if result.attempts >= settings.retries:
                self._baseline_completed.add(key)
        self._qualified[key] = result.strict_eligible
        self._wire_results[key] = serialize_result(result)
        await self._notify()

    async def _run_baseline(self) -> None:
        async def test(proxy):
            key = proxy.key()
            result = self._results.get(key)
            remaining = self.config.testing.retries - (result.attempts if result else 0)
            for _ in range(remaining):
                await self._strict_attempt(proxy, finalist=False)

        proxies = [
            proxy
            for proxy in self._proxies
            if not self.config.scan.prefilter or self._prefilter[proxy.key()]["passed"]
        ]
        await self._pool(proxies, mtproto_worker_count(self.config.testing, len(proxies)), test)
        baseline = score_all(list(self._results.values()), self.config.scoring)
        ranked = rank([result for result in baseline if result.strict_eligible])
        self._finalist_keys = [result.proxy.key() for result in ranked[: self.config.scan.finalists]]
        self._finalist_attempts = dict.fromkeys(self._finalist_keys, 0)
        self._stage = "finalists"
        await self._notify()

    async def _run_finalists(self) -> None:
        scan = self.config.scan

        async def test(proxy):
            key = proxy.key()
            while self._finalist_attempts[key] < scan.finalist_retries:
                # The baseline also created a fresh connection; allow it to
                # settle before reconnecting for the first sustained attempt.
                if scan.finalist_reconnect_delay_seconds:
                    await asyncio.sleep(scan.finalist_reconnect_delay_seconds)
                await self._strict_attempt(proxy, finalist=True)

        proxies = [self._results[key].proxy for key in self._finalist_keys]
        workers = worker_count(
            min(scan.finalist_workers, self.config.testing.mtproto_workers, self.config.testing.workers),
            len(proxies),
        )
        await self._pool(proxies, workers, test)

    async def run_async(self, proxies: list[Proxy]) -> list[ProxyResult]:
        if self.config.testing.mtproto_profile != "telegram_session":
            raise ValueError("Staged tests require the telegram_session profile")
        self.partial_results = []
        self._callback_lock = asyncio.Lock()
        self._restore(proxies)
        complete = self._stage == "complete"
        stage_before_completion = self._stage
        try:
            await self._notify()
            if self._stage == "prefilter":
                await self._run_prefilter()
            if self._stage == "baseline":
                await self._run_baseline()
            if self._stage == "finalists":
                await self._run_finalists()
            stage_before_completion = self._stage
            self._set_policies(completed=True)
            self._stage = "complete"
            await self._notify()
            complete = True
            return [self._results[p.key()] for p in self._proxies if p.key() in self._results]
        finally:
            if not complete:
                if self._stage == "complete":
                    self._stage = stage_before_completion
                self._set_policies(completed=False)
            self.partial_results = [
                replace(self._results[proxy.key()], samples=list(self._results[proxy.key()].samples))
                for proxy in self._proxies
                if proxy.key() in self._results and self._results[proxy.key()].samples
            ]

    def run(self, proxies: list[Proxy]) -> list[ProxyResult]:
        try:
            return asyncio.run(self.run_async(proxies))
        except (KeyboardInterrupt, asyncio.CancelledError) as interrupted:
            raise ScanInterrupted(self.partial_results) from interrupted
