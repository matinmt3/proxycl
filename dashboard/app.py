"""Optional FastAPI adapter for desktop/container dashboard deployments."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response

from dashboard import data

app = FastAPI(title="REVMAMAD — Proxy Radar", version="2.0.0")
DATA_FILE = data.DEFAULT_DATA_FILE
ASSETS = Path(__file__).resolve().parent


@app.get("/api/proxies")
def api_proxies(
    search: str = "",
    sort_by: Literal["score", "avg_latency_ms", "success_rate", "stability"] = "score",
    order: Literal["asc", "desc"] = "desc",
    limit: int = Query(200, ge=1, le=5000),
):
    return data.proxies(DATA_FILE, search, sort_by, order, limit)


@app.get("/api/stats")
def api_stats():
    return data.stats(DATA_FILE)


@app.get("/", response_class=HTMLResponse)
def dashboard_home():
    return (ASSETS / "index.html").read_text(encoding="utf-8")


@app.get("/dashboard.js")
def dashboard_script():
    return Response(
        (ASSETS / "dashboard.js").read_text(encoding="utf-8"), media_type="application/javascript"
    )
