"""Fetches proxies from all enabled sources, merges and deduplicates them."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Dict, List

import httpx

from collector.sources import parse_json_feed, parse_text_blob
from config.loader import AppConfig, SourceConfig
from utils.models import Proxy

logger = logging.getLogger("mtselector.collector")
MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_REQUESTS = 8


class Collector:
    def __init__(self, config: AppConfig):
        self.config = config
        self.source_reports: list[dict] = []

    async def _fetch_url(self, client: httpx.AsyncClient, url: str) -> str:
        async with client.stream("GET", url, timeout=15.0, follow_redirects=True) as resp:
            resp.raise_for_status()
            chunks = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise ValueError("Source exceeds the 4 MiB download limit")
                chunks.append(chunk)
            body = b"".join(chunks)
            encoding = resp.encoding or "utf-8"
            if body.startswith(b"\xef\xbb\xbf"):
                encoding = "utf-8-sig"
            return body.decode(encoding, errors="replace")

    async def _read_local_file(self, url: str) -> str:
        # supports file:// paths for manual/offline proxy lists
        path = url.replace("file://", "", 1)
        p = Path(path)

        def read() -> str:
            with p.open("rb") as handle:
                body = handle.read(MAX_SOURCE_BYTES + 1)
            if len(body) > MAX_SOURCE_BYTES:
                raise ValueError("Local source exceeds the 4 MiB size limit")
            return body.decode("utf-8-sig")

        return await asyncio.to_thread(read)

    async def _fetch_source(
        self, client: httpx.AsyncClient, source: SourceConfig, report: dict | None = None
    ) -> List[Proxy]:
        try:
            if source.url.startswith("file://"):
                text = await self._read_local_file(source.url)
            else:
                text = await self._fetch_url(client, source.url)
            if source.type in ("http_json", "json_feed", "github_raw"):
                proxies = parse_json_feed(text, source.name)
            else:
                proxies = parse_text_blob(text, source.name)
        except Exception as exc:  # noqa: BLE001 - a bad source must not kill the whole run
            # HTTP exception text may contain private feed credentials in its URL.
            detail = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                detail += f" (HTTP {exc.response.status_code})"
            logger.warning("Source '%s' failed: %s", source.name, detail)
            if report is not None:
                report.update(status="error", count=0, error=detail)
            return []

        logger.info("Source '%s' -> %d proxies", source.name, len(proxies))
        if report is not None:
            report.update(status="ok" if proxies else "empty", count=len(proxies))
        return proxies

    async def collect_async(self) -> List[Proxy]:
        self.source_reports = []
        enabled = [s for s in self.config.sources if s.enabled]
        if not enabled:
            logger.warning("No enabled sources configured.")
            return []

        semaphore = asyncio.Semaphore(MAX_SOURCE_REQUESTS)
        reports = [{"name": source.name, "status": "empty", "count": 0} for source in enabled]

        async with httpx.AsyncClient(
            headers={"User-Agent": "Revmamad/3.0 (MTProto proxy collector)"}
        ) as client:

            async def fetch(source: SourceConfig, report: dict) -> List[Proxy]:
                async with semaphore:
                    return await self._fetch_source(client, source, report)

            results = await asyncio.gather(
                *(fetch(source, report) for source, report in zip(enabled, reports))
            )

        # Publish only a completed collection, in config order regardless of
        # completion timing. Cancellation keeps incomplete reports unpublished.
        self.source_reports = reports
        merged: Dict[str, Proxy] = {}
        for group in results:
            for proxy in group:
                merged.setdefault(proxy.key(), proxy)

        logger.info("Collected %d unique proxies from %d sources", len(merged), len(enabled))
        return list(merged.values())

    def collect(self) -> List[Proxy]:
        return asyncio.run(self.collect_async())
