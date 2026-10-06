"""Runs the collect -> test -> score -> export pipeline on a schedule."""
from __future__ import annotations

import logging
import time

from config.loader import AppConfig

logger = logging.getLogger("mtselector.scheduler")


class Scheduler:
    def __init__(self, config: AppConfig, pipeline_fn):
        """pipeline_fn: a zero-arg callable that runs one full collect/test/export cycle."""
        self.config = config
        self.pipeline_fn = pipeline_fn

    def run_once(self):
        logger.info("Running one pipeline cycle...")
        self.pipeline_fn()

    def run_continuous(self):
        """Loop forever, running back-to-back with no delay between cycles."""
        logger.info("Starting continuous mode (back-to-back cycles).")
        while True:
            start = time.time()
            self.pipeline_fn()
            elapsed = time.time() - start
            logger.info("Cycle finished in %.1fs, starting next cycle immediately.", elapsed)

    def run_scheduled(self):
        """Loop forever, running once every `interval_minutes`."""
        interval = max(1, self.config.scheduler.interval_minutes) * 60
        logger.info("Starting scheduler mode: every %d minutes.", self.config.scheduler.interval_minutes)
        while True:
            start = time.time()
            self.pipeline_fn()
            elapsed = time.time() - start
            sleep_for = max(0.0, interval - elapsed)
            logger.info("Cycle finished in %.1fs. Sleeping %.1fs until next run.", elapsed, sleep_for)
            time.sleep(sleep_for)

    def start(self):
        mode = self.config.scheduler.mode
        if mode == "manual":
            self.run_once()
        elif mode == "continuous":
            self.run_continuous()
        elif mode == "scheduler":
            self.run_scheduled()
        else:
            raise ValueError(f"Unknown scheduler mode: {mode}")
