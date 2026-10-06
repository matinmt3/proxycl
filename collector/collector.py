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


class Collector:
    def __init__(self, config: AppConfig):
        self.config = config

    async def _fetch_url(self, client: httpx.AsyncClient, url: str) -> str:
        resp = await client.get(url, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.text

    async def _read_local_file(self, url: str) -> str:
        # supports file:// paths for manual/offline proxy lists
        path = url.replace("file://", "", 1)
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Local source file not found: {path}")
        return p.read_text(encoding="utf-8")

    async def _fetch_source(self, client: httpx.AsyncClient, source: SourceConfig) -> List[Proxy]:
        try:
            if source.url.startswith("file://"):
                text = await self._read_local_file(source.url)
            else:
                text = await self._fetch_url(client, source.url)
        except Exception as exc:  # noqa: BLE001 - a bad source must not kill the whole run
            logger.warning("Source '%s' failed: %s", source.name, exc)
            return []

        if source.type in ("http_json", "json_feed"):
            proxies = parse_json_feed(text, source.name)
        else:
            proxies = parse_text_blob(text, source.name)

        logger.info("Source '%s' -> %d proxies", source.name, len(proxies))
        return proxies

    async def collect_async(self) -> List[Proxy]:
        enabled = [s for s in self.config.sources if s.enabled]
        if not enabled:
            logger.warning("No enabled sources configured.")
            return []

        async with httpx.AsyncClient(headers={"User-Agent": "Mozilla/5.0 (Telegram-MTProto-Smart-Selector)"}) as client:
            results = await asyncio.gather(*(self._fetch_source(client, s) for s in enabled))

        merged: Dict[str, Proxy] = {}
        for group in results:
            for proxy in group:
                merged.setdefault(proxy.key(), proxy)

        logger.info("Collected %d unique proxies from %d sources", len(merged), len(enabled))
        return list(merged.values())

    def collect(self) -> List[Proxy]:
        return asyncio.run(self.collect_async())
