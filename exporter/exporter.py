"""Exports ranked proxy results into every required output format."""

from __future__ import annotations

import csv
import json
import logging
import math
import os
import tempfile
from io import StringIO
from pathlib import Path
from typing import List

from config.loader import OutputConfig
from telegram_webproxy.models import TelegramWebProxy
from utils.models import Proxy, ProxyResult

logger = logging.getLogger("mtselector.exporter")


def eligible_results(
    results: List[ProxyResult], output_cfg: OutputConfig, *, now: float | None = None
) -> List[ProxyResult]:
    return [
        r
        for r in results
        if (
            not isinstance(r.proxy, (Proxy, TelegramWebProxy))
            or (
                r.quality_eligible
                and r.strict_successes >= output_cfg.mtproto_min_successes
                and r.strict_successes / max(1, r.attempts) >= output_cfg.mtproto_min_success_rate
                and r.telegram_p95_ms <= output_cfg.mtproto_max_ping_p95_ms
                and r.telegram_jitter_ms <= output_cfg.mtproto_max_ping_jitter_ms
            )
        )
        and r.evidence_fresh(now=now, max_age_seconds=output_cfg.proof_max_age_minutes * 60)
        and r.successes > 0
        and math.isfinite(r.score)
        and r.score >= output_cfg.min_score
        and r.ranking_latency_ms is not None
        and math.isfinite(r.ranking_latency_ms)
        and 0 <= r.ranking_latency_ms <= output_cfg.max_latency_ms
    ]


class Exporter:
    link_field = "tg_link"
    caption = "MTProto"

    def connection_link(self, result: ProxyResult) -> str:
        return result.proxy.tg_link()

    def __init__(self, output_cfg: OutputConfig):
        self.output_cfg = output_cfg
        self.folder = Path(output_cfg.folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _filtered(self, results: List[ProxyResult]) -> List[ProxyResult]:
        return eligible_results(results, self.output_cfg)

    def _write(self, filename: str, text: str) -> Path:
        if Path(filename).name != filename or filename in ("", ".", ".."):
            raise ValueError("Export filename must be a filename inside the output directory")
        path = self.folder / filename
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=self.folder, prefix=".revmamad-", delete=False
            ) as stream:
                temporary = Path(stream.name)
                stream.write(text)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path

    def export_json(self, results: List[ProxyResult], filename: str = "proxy.json") -> Path:
        data = [r.to_dict() for r in self._filtered(results)]
        return self._write(filename, json.dumps(data, indent=2, allow_nan=False))

    def export_txt(self, results: List[ProxyResult], filename: str = "proxy.txt") -> Path:
        lines = [
            f"{'[' + r.proxy.server + ']' if ':' in r.proxy.server else r.proxy.server}:{r.proxy.port}:{r.proxy.secret}  # score={r.score:.3f}"
            for r in self._filtered(results)
        ]
        return self._write(filename, "\n".join(lines) + ("\n" if lines else ""))

    def export_csv(self, results: List[ProxyResult], filename: str = "proxy.csv") -> Path:
        fieldnames = [
            "server",
            "port",
            "secret",
            "source",
            "score",
            "report_schema",
            "ranking_policy",
            "evidence_type",
            "evidence_status",
            "oldest_observed_at",
            "latest_observed_at",
            "proof_max_age_seconds",
            "evidence_expires_at",
            "result_status",
            "provisional",
            "within_session_jitter_ms",
            "interconnection_drift_ms",
            "success_rate",
            "avg_latency_ms",
            "median_latency_ms",
            "jitter_ms",
            "stability",
            "timeout_rate",
            "last_failure_reason",
            "verification_profile",
            "strict_eligible",
            "strict_successes",
            "dc_ids_verified",
            "ping_count",
            "telegram_median_ms",
            "telegram_p95_ms",
            "telegram_jitter_ms",
            "reliability_lower_bound",
            "strict_policy",
            self.link_field,
        ]
        with StringIO(newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for r in self._filtered(results):
                d = r.to_dict()
                for key in ("dc_ids_verified", "strict_policy"):
                    d[key] = json.dumps(d.get(key), separators=(",", ":"))
                writer.writerow({k: d.get(k) for k in fieldnames})
            return self._write(filename, f.getvalue())

    def export_telegram_links(self, results: List[ProxyResult], filename: str = "telegram_links.txt") -> Path:
        lines = [self.connection_link(r) for r in self._filtered(results)]
        return self._write(filename, "\n".join(lines) + ("\n" if lines else ""))

    def export_top_n(self, results: List[ProxyResult], n: int) -> Path:
        if n < 1:
            raise ValueError("Top count must be positive")
        from benchmark.scorer import rank

        data = [r.to_dict() for r in rank(self._filtered(results))[:n]]
        return self._write(f"top{n}.json", json.dumps(data, indent=2, allow_nan=False))

    def export_best_n_readable(
        self, results: List[ProxyResult], n: int = 10, filename: str = "best10_links.txt"
    ) -> Path:
        """Human-friendly top-N file: rank, speed, and a ready-to-click tg:// link for each proxy.

        Use the same score/latency/endpoint order as top10.json and terminal output.
        """
        if n < 1:
            raise ValueError("Top count must be positive")
        healthy = self._filtered(results)
        from benchmark.scorer import rank

        top = rank(healthy)[:n]

        lines = [
            "Top {} Verified {} Proxies (up to {} requested)".format(len(top), self.caption, n),
            "Generated by REVMAMAD",
            "=" * 60,
            "",
        ]
        if any(result.provisional for result in top):
            lines.extend(["PROVISIONAL results: the scan stopped before completing its full test plan.", ""])
        for i, r in enumerate(top, 1):
            lines.append(
                f"#{i}  {r.proxy.server}:{r.proxy.port}\n"
                f"    Telegram RTT p95: {r.telegram_p95_ms:.0f} ms | Median: {r.telegram_median_ms:.0f} ms | Jitter: {r.telegram_jitter_ms:.0f} ms\n"
                f"    Sessions: {r.strict_successes}/{r.attempts} | Matched pings: {r.ping_count} | Success: {r.success_rate * 100:.0f}% | Score: {r.score * 100:.1f}%\n"
                f"    Setup: {r.avg_latency_ms:.0f} ms\n"
                f"    Result: {'PROVISIONAL' if r.provisional else 'complete'}\n"
                f"    Policy: {r.ranking_policy} | Evidence: {r.freshness_status} | Expires (UTC epoch): {r.evidence_expires_at:.0f}\n"
                f"    Link:  {self.connection_link(r)}\n"
            )

        if not top:
            lines.append("No healthy proxies found in the last run. Run `python main.py export` again later.")

        return self._write(filename, "\n".join(lines))

    def export_all(self, results: List[ProxyResult]) -> dict:
        ranked = self._filtered(results)
        from benchmark.scorer import rank

        ranked = rank(ranked)

        paths = {
            "json": str(self.export_json(ranked)),
            "txt": str(self.export_txt(ranked)),
            "csv": str(self.export_csv(ranked)),
            "telegram_links": str(self.export_telegram_links(ranked)),
        }
        for size in self.output_cfg.top_sizes:
            paths[f"top{size}"] = str(self.export_top_n(ranked, size))

        logger.info("Exported %d healthy proxies to %s", len(ranked), self.folder)
        return paths
