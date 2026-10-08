"""Optional FastAPI adapter with the same read-only snapshot APIs as Termux."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from dashboard import data

app = FastAPI(title="REVMAMAD — Proxy Radar", version="3.0.0")
OUTPUT_FOLDER = Path(os.environ.get("REVMAMAD_OUTPUT_FOLDER", data.DEFAULT_OUTPUT_FOLDER))
DATA_FILE = data.DEFAULT_DATA_FILE  # compatibility for v2 integrations
ASSETS = Path(__file__).resolve().parent


def _api(route: str, request: Request):
    folder = Path(DATA_FILE).parent if DATA_FILE != data.DEFAULT_DATA_FILE else OUTPUT_FOLDER
    params = {key: request.query_params.getlist(key) for key in request.query_params}
    try:
        result = data.api_response(route, folder, params)
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="Invalid query parameters") from exc
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@app.get("/api/snapshot")
def api_snapshot(request: Request, mode: Literal["mtproto", "web"] = "mtproto"):
    return _api("/api/snapshot", request)


@app.get("/api/proxies")
def api_proxies(request: Request, mode: Literal["mtproto", "web"] = "mtproto"):
    return _api("/api/proxies", request)


@app.get("/api/stats")
def api_stats(request: Request, mode: Literal["mtproto", "web"] = "mtproto"):
    return _api("/api/stats", request)


@app.get("/", response_class=HTMLResponse)
def dashboard_home():
    return (ASSETS / "index.html").read_text(encoding="utf-8")


@app.get("/dashboard.js")
def dashboard_script():
    return Response(
        (ASSETS / "dashboard.js").read_text(encoding="utf-8"), media_type="application/javascript"
    )


@app.get("/styles.css")
def dashboard_styles():
    return Response((ASSETS / "styles.css").read_text(encoding="utf-8"), media_type="text/css")
