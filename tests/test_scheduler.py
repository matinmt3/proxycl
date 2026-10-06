from types import SimpleNamespace

import pytest

from config.loader import SchedulerConfig
from scheduler.scheduler import Scheduler


def AppConfig():
    return SimpleNamespace(scheduler=SchedulerConfig())


def test_manual_runs_once():
    calls = []
    Scheduler(AppConfig(), lambda: calls.append(True)).start()
    assert calls == [True]


def test_scheduled_uses_elapsed_time(monkeypatch):
    import scheduler.scheduler as module

    clock = iter([100, 107])
    sleeps = []
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))

    def stop(delay):
        sleeps.append(delay)
        raise KeyboardInterrupt

    monkeypatch.setattr(module.time, "sleep", stop)
    cfg = AppConfig()
    cfg.scheduler.interval_minutes = 2
    calls = []
    with pytest.raises(KeyboardInterrupt):
        Scheduler(cfg, lambda: calls.append(True)).run_scheduled()
    assert calls == [True] and sleeps == [113]


def test_continuous_and_error_propagation(monkeypatch):
    import scheduler.scheduler as module

    monkeypatch.setattr(module.time, "monotonic", lambda: 10)
    count = 0

    def cycle():
        nonlocal count
        count += 1
        if count == 2:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        Scheduler(AppConfig(), cycle).run_continuous()
    assert count == 2
    cfg = AppConfig()
    cfg.scheduler.mode = "invalid"
    with pytest.raises(ValueError):
        Scheduler(cfg, cycle).start()
