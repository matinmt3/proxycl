"""Load and validate config.yaml into typed structures."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional
from urllib.parse import unquote


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
        total = self.latency_weight + self.success_rate_weight + self.stability_weight + self.timeout_weight
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


def _section(raw: dict, name: str, model):
    value = raw.get(name)
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a YAML mapping")
    try:
        return model(**value)
    except TypeError as exc:
        raise ValueError(f"Invalid {name} settings: {exc}") from exc


def _number(value, name: str, *, positive: bool = False, integer: bool = False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if integer and not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if positive and value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def _text(value, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _absolute_path(value: str, base: Path) -> str:
    path = Path(value).expanduser()
    return str((path if path.is_absolute() else base / path).resolve())


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
    def load(cls, path: str | Path | None = None) -> "AppConfig":
        # The built-in config works even when main.py is invoked from another directory.
        p = Path(path).expanduser() if path is not None else Path(__file__).with_name("config.yaml")
        p = p.resolve()
        if not p.exists():
            raise FileNotFoundError(f"Config file not found: {p}")
        import yaml

        with p.open("r", encoding="utf-8") as f:
            try:
                raw = yaml.safe_load(f)
            except yaml.YAMLError as exc:
                raise ValueError(f"Invalid YAML in {p}: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ValueError("The config must contain a YAML mapping")
        allowed = {"sources", "testing", "scoring", "output", "scheduler", "dashboard", "logging"}
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"Unknown config section(s): {', '.join(str(key) for key in unknown)}")

        source_data = raw.get("sources")
        if source_data is None:
            source_data = []
        if not isinstance(source_data, list):
            raise ValueError("sources must be a YAML list")
        sources = []
        for index, data in enumerate(source_data, 1):
            if not isinstance(data, dict):
                raise ValueError(f"sources[{index}] must be a YAML mapping")
            try:
                source = SourceConfig(**data)
            except TypeError as exc:
                raise ValueError(f"Invalid sources[{index}]: {exc}") from exc
            source.name = _text(source.name, f"sources[{index}].name")
            source.url = _text(source.url, f"sources[{index}].url")
            source.type = _text(source.type, f"sources[{index}].type")
            if source.type not in {"github_raw", "http_txt", "http_json", "json_feed", "telegram_channel"}:
                raise ValueError(f"Unsupported source type: {source.type}")
            if not isinstance(source.enabled, bool):
                raise ValueError(f"sources[{index}].enabled must be true or false (without quotes)")
            sources.append(source)

        testing = _section(raw, "testing", TestingConfig)
        scoring = _section(raw, "scoring", ScoringConfig)
        output = _section(raw, "output", OutputConfig)
        scheduler = _section(raw, "scheduler", SchedulerConfig)
        dashboard = _section(raw, "dashboard", DashboardConfig)
        logging_cfg = _section(raw, "logging", LoggingConfig)

        for name in ("workers", "retries"):
            _number(getattr(testing, name), f"testing.{name}", positive=True, integer=True)
        for name in ("timeout_seconds", "connect_timeout_seconds"):
            _number(getattr(testing, name), f"testing.{name}", positive=True)
        for name in ("latency_weight", "success_rate_weight", "stability_weight", "timeout_weight"):
            if _number(getattr(scoring, name), f"scoring.{name}") < 0:
                raise ValueError(f"scoring.{name} cannot be negative")
        if (
            sum(
                getattr(scoring, name)
                for name in ("latency_weight", "success_rate_weight", "stability_weight", "timeout_weight")
            )
            <= 0
        ):
            raise ValueError("At least one scoring weight must be greater than zero")
        _number(scoring.max_latency_ms, "scoring.max_latency_ms", positive=True)
        _number(output.max_latency_ms, "output.max_latency_ms", positive=True)
        if not 0 <= _number(output.min_score, "output.min_score") <= 1:
            raise ValueError("output.min_score must be between 0 and 1")
        if not isinstance(output.top_sizes, list):
            raise ValueError("output.top_sizes must be a list of positive integers")
        for size in output.top_sizes:
            _number(size, "output.top_sizes entry", positive=True, integer=True)
        output.top_sizes = list(dict.fromkeys(output.top_sizes))
        if scheduler.mode not in {"manual", "continuous", "scheduler"}:
            raise ValueError("scheduler.mode must be manual, continuous, or scheduler")
        _number(scheduler.interval_minutes, "scheduler.interval_minutes", positive=True, integer=True)
        dashboard.host = _text(dashboard.host, "dashboard.host")
        _number(dashboard.port, "dashboard.port", positive=True, integer=True)
        if dashboard.port > 65535:
            raise ValueError("dashboard.port must be between 1 and 65535")
        logging_cfg.level = _text(logging_cfg.level, "logging.level").upper()
        if logging_cfg.level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "NOTSET"}:
            raise ValueError("logging.level must be DEBUG, INFO, WARNING, ERROR, CRITICAL, or NOTSET")

        # Match the repository's config/config.yaml layout; external configs resolve
        # their paths against their own containing directory.
        base = p.parent.parent if p.parent.name == "config" and p.name == "config.yaml" else p.parent
        output.folder = _absolute_path(_text(output.folder, "output.folder"), base)
        logging_cfg.log_dir = _absolute_path(_text(logging_cfg.log_dir, "logging.log_dir"), base)
        for source in sources:
            if source.url.startswith("file://"):
                local = unquote(source.url[len("file://") :])
                if not local:
                    raise ValueError(f"Empty local file path for source: {source.name}")
                source.url = "file://" + _absolute_path(local, base)

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
