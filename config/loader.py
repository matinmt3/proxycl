"""Load and validate config.yaml into typed structures."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class SourceConfig:
    name: str
    type: str
    url: str
    enabled: bool = True


@dataclass
class TestingConfig:
    workers: int = 200
    timeout_seconds: float = 5.0
    retries: int = 3
    connect_timeout_seconds: float = 3.0


@dataclass
class ScoringConfig:
    latency_weight: float = 0.35
    success_rate_weight: float = 0.35
    stability_weight: float = 0.20
    timeout_weight: float = 0.10
    max_latency_ms: float = 2000.0

    def normalized(self) -> "ScoringConfig":
        total = (
            self.latency_weight
            + self.success_rate_weight
            + self.stability_weight
            + self.timeout_weight
        )
        if total <= 0:
            return ScoringConfig()
        return ScoringConfig(
            latency_weight=self.latency_weight / total,
            success_rate_weight=self.success_rate_weight / total,
            stability_weight=self.stability_weight / total,
            timeout_weight=self.timeout_weight / total,
            max_latency_ms=self.max_latency_ms,
        )


@dataclass
class OutputConfig:
    folder: str = "output"
    min_score: float = 0.0
    max_latency_ms: float = 5000.0
    top_sizes: List[int] = field(default_factory=lambda: [10, 50, 100])


@dataclass
class SchedulerConfig:
    mode: str = "manual"
    interval_minutes: int = 15


@dataclass
class DashboardConfig:
    host: str = "0.0.0.0"
    port: int = 8000


@dataclass
class LoggingConfig:
    level: str = "INFO"
    log_dir: str = "logs"


@dataclass
class AppConfig:
    sources: List[SourceConfig]
    testing: TestingConfig
    scoring: ScoringConfig
    output: OutputConfig
    scheduler: SchedulerConfig
    dashboard: DashboardConfig
    logging: LoggingConfig
    _path: Optional[Path] = None

    @classmethod
    def load(cls, path: str = "config/config.yaml") -> "AppConfig":
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        sources = [SourceConfig(**s) for s in raw.get("sources", [])]
        testing = TestingConfig(**raw.get("testing", {}))
        scoring = ScoringConfig(**raw.get("scoring", {}))
        output = OutputConfig(**raw.get("output", {}))
        scheduler = SchedulerConfig(**raw.get("scheduler", {}))
        dashboard = DashboardConfig(**raw.get("dashboard", {}))
        logging_cfg = LoggingConfig(**raw.get("logging", {}))

        return cls(
            sources=sources,
            testing=testing,
            scoring=scoring,
            output=output,
            scheduler=scheduler,
            dashboard=dashboard,
            logging=logging_cfg,
            _path=p,
        )
