"""Bounded feed retrieval with isolated failures and stable source reports."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from webproxy.models import WebProxy
from webproxy.sources import parse_json_feed, parse_text_blob

if TYPE_CHECKING:
    from config.loader import AppConfig

MAX_SOURCE_BYTES = 4 * 1024 * 1024
MAX_SOURCE_REQUESTS = 8
logger = logging.getLogger("mtselector.web.collector")


class WebCollector:
    def __init__(self, config: AppConfig):
        self.config = config
        self.source_reports: list[dict] = []

    async def _fetch_url(self, client, url: str) -> str:
        async with client.stream("GET", url, timeout=15.0, follow_redirects=True) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_SOURCE_BYTES:
                    raise ValueError("Source exceeds download limit")
                body.extend(chunk)
            encoding = "utf-8-sig" if body.startswith(b"\xef\xbb\xbf") else response.encoding or "utf-8"
            return body.decode(encoding, errors="replace")

    async def _read_local_file(self, url: str) -> str:
        def read() -> str:
            with Path(url[len("file://") :]).open("rb") as handle:
                body = handle.read(MAX_SOURCE_BYTES + 1)
            if len(body) > MAX_SOURCE_BYTES:
                raise ValueError("Source exceeds download limit")
            return body.decode("utf-8-sig")

        return await asyncio.to_thread(read)

    async def collect_async(self) -> list[WebProxy]:
        import httpx

        enabled = [s for s in self.config.web_sources if s.enabled]
        self.source_reports = []
        semaphore = asyncio.Semaphore(MAX_SOURCE_REQUESTS)
        async with httpx.AsyncClient(headers={"User-Agent": "Revmamad/3.0 (Web proxy collector)"}) as client:

            async def fetch(source):
                async with semaphore:
                    try:
                        text = (
                            await self._read_local_file(source.url)
                            if source.url.startswith("file://")
                            else await self._fetch_url(client, source.url)
                        )
                        parser = (
                            parse_json_feed
                            if source.type in {"http_json", "json_feed", "github_raw"}
                            else parse_text_blob
                        )
                        proxies = parser(text, source.name, source.protocol)
                        return proxies, {
                            "name": source.name,
                            "status": "ok" if proxies else "empty",
                            "count": len(proxies),
                        }
                    except Exception as exc:
                        detail = type(exc).__name__
                        if isinstance(exc, httpx.HTTPStatusError):
                            detail += f" (HTTP {exc.response.status_code})"
                        logger.warning("Web source '%s' failed: %s", source.name, detail)
                        return [], {"name": source.name, "status": "error", "count": 0, "error": detail}

            groups = await asyncio.gather(*(fetch(source) for source in enabled))
        unique = {}
        for proxies, report in groups:
            self.source_reports.append(report)
            for proxy in proxies:
                unique.setdefault(proxy.key(), proxy)
        return list(unique.values())

    def collect(self) -> list[WebProxy]:
        return asyncio.run(self.collect_async())
