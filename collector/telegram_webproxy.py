"""Collect official WEB share links without treating HTTP/SOCKS as Telegram WEB."""

import json
import logging
from dataclasses import replace

import httpx

from collector.collector import Collector
from collector.health import SourceHealth
from telegram_webproxy.parser import extract_links

logger = logging.getLogger("mtselector.collector.webproxy")


def parse_feed(text, source):
    try:
        values = [json.loads(text)]
    except (ValueError, RecursionError):
        return extract_links(text, source)
    found = {}
    while values:
        value = values.pop()
        if isinstance(value, str):
            for proxy in extract_links(value, source):
                found.setdefault(proxy.key(), proxy)
        elif isinstance(value, list):
            values.extend(reversed(value))
        elif isinstance(value, dict):
            values.extend(reversed(list(value.values())))
    return list(found.values())


class TelegramWebCollector(Collector):
    def __init__(self, config):
        prepared = replace(config, sources=config.webproxy_sources)
        for name in ("_scan_controller", "_scan_collection_deadline"):
            if hasattr(config, name):
                setattr(prepared, name, getattr(config, name))
        super().__init__(prepared)
        self.health = SourceHealth(config, "webproxy")

    async def _fetch_source(self, client, source, report=None):
        try:
            text = (
                await self._read_local_file(source.url)
                if source.url.startswith("file://")
                else await self._fetch_url(client, source.url)
            )
            candidates = parse_feed(text, source.name)
        except Exception as exc:
            detail = type(exc).__name__
            if isinstance(exc, httpx.HTTPStatusError):
                detail += f" (HTTP {exc.response.status_code})"
            logger.warning("WEB source failed: %s", detail)
            if report is not None:
                report.update(status="error", count=0, error=detail)
                report["_health_network_error"] = isinstance(exc, (httpx.HTTPError, OSError))
            return []
        if report is not None:
            report.update(status="ok" if candidates else "empty", count=len(candidates))
        return candidates
