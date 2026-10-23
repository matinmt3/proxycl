"""Thread-safe stop requests and bounded asynchronous work admission.

Only local event-loop pressure changes concurrency. A slow or failed proxy is
never evidence that the phone is overloaded. No cancelled probe is converted
into a fabricated failed measurement.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import os
import threading
import time
from collections.abc import Awaitable, Callable


def scan_deadline(config, *, started: float | None = None) -> float | None:
    """Bound an engine's budget by the orchestration's original deadline.

    Collection, checkpoint setup and printing can consume time before an engine
    starts. That elapsed time must never grant the engine another full budget.
    Standalone engines still get their configured duration from their own start.
    """
    started = time.monotonic() if started is None else started
    duration = getattr(getattr(config, "scan", None), "time_budget_seconds", None)
    local = None if duration is None else started + duration
    absolute = getattr(config, "_scan_deadline", None)
    if absolute is not None:
        if type(absolute) not in (int, float) or not math.isfinite(absolute):
            raise ValueError("Invalid absolute scan deadline")
        return min(local, absolute) if local is not None else absolute
    return local


class ScanStopped(asyncio.CancelledError):
    """A user stop or an elapsed time budget cancelled unfinished work."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ScanController:
    """One scan's stop token, safe to trigger from a dashboard server thread."""

    def __init__(self, stop_event: threading.Event | None = None):
        self._event = stop_event if stop_event is not None else threading.Event()
        self._lock = threading.Lock()
        self._reason: str | None = None

    @property
    def stop_requested(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def request_stop(self, reason: str = "user") -> None:
        if reason not in {"user", "budget", "shutdown"}:
            raise ValueError("Unknown scan stop reason")
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._event.set()

    def check(self) -> None:
        """Reject admission immediately after a stop, without waiting for polling."""
        if self.stop_requested:
            raise ScanStopped(self.reason or "user")

    async def run(self, factory: Callable[[], Awaitable], *, deadline: float | None = None):
        """Run one bounded operation; drain it before propagating a stop.

        A factory avoids constructing a coroutine that would never be awaited
        when stop was already requested. Deadlines use time.monotonic().
        """
        self.check()
        if deadline is not None and time.monotonic() >= deadline:
            raise ScanStopped("budget")
        task = asyncio.ensure_future(factory())
        try:
            while True:
                remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
                timeout = 0.05 if remaining is None else min(0.05, remaining)
                done, _ = await asyncio.wait({task}, timeout=timeout)
                # Finished evidence wins a tie with the deadline or stop event.
                if task in done:
                    return task.result()
                if self.stop_requested:
                    raise ScanStopped(self.reason or "user")
                if deadline is not None and time.monotonic() >= deadline:
                    raise ScanStopped("budget")
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class AdaptiveLimiter:
    """A bounded pool gate with a conservative initial cap on Termux.

    Two consecutive event-loop stalls lower admission; ten calm observations
    recover one slot. Existing in-flight work drains normally. The monitor does
    not read latency, failures, battery state, carrier identity or credentials.
    """

    sample_interval = 0.2
    pressure_threshold = 0.1

    def __init__(self, workers: int, *, enabled: bool = True):
        self.maximum = max(1, workers)
        self.enabled = enabled
        self.effective = self.maximum
        self.reason = "configured_limit"
        termux = os.environ.get("TERMUX_VERSION") or "/com.termux/" in os.environ.get("PREFIX", "")
        if enabled and termux:
            self.effective = min(self.maximum, max(2, min(8, (os.cpu_count() or 1) * 2)))
            if self.effective < self.maximum:
                self.reason = "termux_initial_cap"
        self.active = 0
        self.last_lag_ms = 0.0
        self._calm = 0
        self._pressure = 0
        self._condition = asyncio.Condition()
        self._monitor: asyncio.Task | None = None

    @property
    def summary(self) -> dict:
        return {
            "configured_workers": self.maximum,
            "effective_workers": self.effective,
            "active_workers": self.active,
            "pressure_reason": self.reason,
            "event_loop_lag_ms": round(self.last_lag_ms, 3),
        }

    async def observe(self, lag: float) -> None:
        """Record a local scheduler delay, independently testable without load."""
        self.last_lag_ms = max(0.0, lag) * 1000
        if not self.enabled:
            return
        async with self._condition:
            if lag >= self.pressure_threshold:
                self._pressure += 1
                self._calm = 0
                if self._pressure >= 2:
                    self.effective = max(1, self.effective // 2)
                    self.reason = "event_loop_pressure"
                    self._pressure = 0
            else:
                self._pressure = 0
                self._calm += 1
                if self._calm >= 10 and self.effective < self.maximum:
                    self.effective += 1
                    self.reason = "recovering"
                    self._calm = 0
                    self._condition.notify_all()

    async def _watch(self) -> None:
        while True:
            started = time.monotonic()
            await asyncio.sleep(self.sample_interval)
            await self.observe(time.monotonic() - started - self.sample_interval)

    async def __aenter__(self):
        if self.enabled:
            self._monitor = asyncio.create_task(self._watch())
        return self

    async def __aexit__(self, *args):
        if self._monitor is not None:
            self._monitor.cancel()
            await asyncio.gather(self._monitor, return_exceptions=True)

    @contextlib.asynccontextmanager
    async def slot(self):
        async with self._condition:
            await self._condition.wait_for(lambda: self.active < self.effective)
            self.active += 1
        try:
            yield
        finally:
            async with self._condition:
                self.active -= 1
                self._condition.notify_all()
