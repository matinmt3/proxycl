"""FastAPI web dashboard for browsing benchmark results."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI(title="Revmamad — Proxy Radar")

DATA_FILE = Path("output/proxy.json")


def _load_data() -> list:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


@app.get("/api/proxies")
def api_proxies(
    search: Optional[str] = Query(None, description="Filter by server/source substring"),
    sort_by: str = Query("score", description="score|avg_latency_ms|success_rate|stability"),
    order: str = Query("desc", description="asc|desc"),
    limit: int = Query(200, ge=1, le=5000),
):
    data = _load_data()

    if search:
        s = search.lower()
        data = [d for d in data if s in d.get("server", "").lower() or s in (d.get("source") or "").lower()]

    reverse = order.lower() != "asc"
    data.sort(key=lambda d: (d.get(sort_by) if d.get(sort_by) is not None else -1), reverse=reverse)

    return JSONResponse(data[:limit])


@app.get("/api/stats")
def api_stats():
    data = _load_data()
    total = len(data)
    healthy = [d for d in data if (d.get("success_rate") or 0) > 0]
    avg_latency = None
    if healthy:
        lats = [d["avg_latency_ms"] for d in healthy if d.get("avg_latency_ms") is not None]
        if lats:
            avg_latency = round(sum(lats) / len(lats), 2)

    best = max(data, key=lambda d: d.get("score", 0), default=None)
    worst = min(data, key=lambda d: d.get("score", 0), default=None)

    return {
        "total_proxies": total,
        "healthy_proxies": len(healthy),
        "average_latency_ms": avg_latency,
        "best_proxy": best,
        "worst_proxy": worst,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard_home():
    return """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Revmamad — Proxy Radar</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;700&family=Space+Grotesk:wght@500;600;700&display=swap');

  :root {
    color-scheme: dark;
    --bg: #0a0f0d;
    --panel: #101815;
    --line: #1c2b25;
    --signal: #39ff9c;
    --signal-dim: #1f8a5c;
    --amber: #ffb454;
    --danger: #ff5f6d;
    --text: #dce8e2;
    --muted: #6f8a7d;
  }
  * { box-sizing: border-box; }
  body {
    font-family: 'Space Grotesk', -apple-system, sans-serif;
    background: radial-gradient(ellipse at top left, #0d1613 0%, var(--bg) 55%);
    color: var(--text);
    margin: 0;
    padding: 28px 32px 60px;
    min-height: 100vh;
  }
  .brand { display:flex; align-items:center; gap:14px; margin-bottom: 4px; }
  .brand svg { flex-shrink:0; }
  .brand h1 {
    font-size: 26px; letter-spacing: 0.5px; margin:0; font-weight:700;
    color: var(--signal); text-shadow: 0 0 18px rgba(57,255,156,0.35);
  }
  .sub { color: var(--muted); margin: 4px 0 28px 46px; font-size: 13px; font-family:'JetBrains Mono',monospace; }

  .cards { display:flex; gap:14px; flex-wrap:wrap; margin-bottom:26px; }
  .card {
    background: var(--panel); border:1px solid var(--line); border-radius:4px;
    padding:14px 20px; min-width:150px; position:relative; overflow:hidden;
  }
  .card::before {
    content:''; position:absolute; top:0; left:0; width:3px; height:100%; background: var(--signal-dim);
  }
  .card .label { font-size:11px; color: var(--muted); font-family:'JetBrains Mono',monospace; text-transform:uppercase; letter-spacing:0.08em; }
  .card .value { font-size:22px; font-weight:700; margin-top:6px; font-family:'JetBrains Mono',monospace; }

  .controls { display:flex; gap:10px; margin-bottom:18px; flex-wrap:wrap; }
  input, select {
    background: var(--panel); border:1px solid var(--line); color: var(--text);
    padding:9px 12px; border-radius:4px; font-family:'JetBrains Mono',monospace; font-size:13px;
  }
  input:focus, select:focus { outline: 1px solid var(--signal); }

  table { width:100%; border-collapse: collapse; font-size:13px; font-family:'JetBrains Mono',monospace; }
  th, td { text-align:left; padding:10px 12px; border-bottom:1px solid var(--line); }
  th { color: var(--muted); cursor:pointer; user-select:none; font-weight:500; text-transform:uppercase; font-size:11px; letter-spacing:0.06em; }
  tr:hover { background: rgba(57,255,156,0.04); }
  .good { color: var(--signal); } .bad { color: var(--danger); } .mid { color: var(--amber); }
  a.connect {
    color: var(--bg); background: var(--signal); padding:4px 10px; border-radius:3px;
    text-decoration:none; font-weight:700; font-size:11px; letter-spacing:0.04em;
  }
  canvas { max-height: 240px; }
  ::selection { background: var(--signal); color:#031008; }
</style>
</head>
<body>
  <div class="brand">
    <svg width="34" height="34" viewBox="0 0 34 34" fill="none">
      <circle cx="17" cy="17" r="15.5" stroke="#1f8a5c" stroke-width="1.5" opacity="0.5"/>
      <circle cx="17" cy="17" r="10" stroke="#39ff9c" stroke-width="1.5" opacity="0.7">
        <animate attributeName="r" values="6;15;6" dur="2.8s" repeatCount="indefinite"/>
        <animate attributeName="opacity" values="0.9;0;0.9" dur="2.8s" repeatCount="indefinite"/>
      </circle>
      <circle cx="17" cy="17" r="3.5" fill="#39ff9c"/>
    </svg>
    <h1>REVMAMAD</h1>
  </div>
  <div class="sub">// proxy radar — live MTProto health &amp; latency scan</div>

  <div class="cards" id="cards"></div>
  <canvas id="chart" style="max-width:900px; margin-bottom:26px;"></canvas>

  <div class="controls">
    <input id="search" placeholder="search server or source..." oninput="load()"/>
    <select id="sortBy" onchange="load()">
      <option value="score">sort: score</option>
      <option value="avg_latency_ms">sort: latency</option>
      <option value="success_rate">sort: success rate</option>
      <option value="stability">sort: stability</option>
    </select>
    <select id="order" onchange="load()">
      <option value="desc">descending</option>
      <option value="asc">ascending</option>
    </select>
  </div>

  <table>
    <thead>
      <tr><th>Server</th><th>Port</th><th>Score</th><th>Success</th><th>Latency</th><th>Stability</th><th>Source</th><th>Link</th></tr>
    </thead>
    <tbody id="rows"></tbody>
  </table>

<script>
let chart;
async function loadStats() {
  const r = await fetch('/api/stats');
  const s = await r.json();
  document.getElementById('cards').innerHTML = `
    <div class="card"><div class="label">Total Proxies</div><div class="value">${s.total_proxies}</div></div>
    <div class="card"><div class="label">Healthy</div><div class="value good">${s.healthy_proxies}</div></div>
    <div class="card"><div class="label">Avg Latency</div><div class="value">${s.average_latency_ms ?? '-'}<span style="font-size:12px;color:var(--muted)"> ms</span></div></div>
    <div class="card"><div class="label">Best</div><div class="value" style="font-size:15px">${s.best_proxy ? s.best_proxy.server : '-'}</div></div>
    <div class="card"><div class="label">Worst</div><div class="value" style="font-size:15px">${s.worst_proxy ? s.worst_proxy.server : '-'}</div></div>
  `;
}

async function load() {
  const search = document.getElementById('search').value;
  const sortBy = document.getElementById('sortBy').value;
  const order = document.getElementById('order').value;
  const r = await fetch(`/api/proxies?search=${encodeURIComponent(search)}&sort_by=${sortBy}&order=${order}&limit=200`);
  const data = await r.json();

  document.getElementById('rows').innerHTML = data.map(p => `
    <tr>
      <td>${p.server}</td>
      <td>${p.port}</td>
      <td>${(p.score*100).toFixed(1)}%</td>
      <td class="${p.success_rate > 0.8 ? 'good' : p.success_rate > 0.4 ? 'mid' : 'bad'}">${(p.success_rate*100).toFixed(0)}%</td>
      <td>${p.avg_latency_ms ?? '-'} ms</td>
      <td>${(p.stability*100).toFixed(0)}%</td>
      <td style="color:var(--muted)">${p.source}</td>
      <td><a class="connect" href="${p.tg_link}">connect</a></td>
    </tr>
  `).join('');

  const top = data.slice(0, 15);
  const ctx = document.getElementById('chart');
  const chartData = {
    labels: top.map(p => p.server),
    datasets: [{ label: 'Avg Latency (ms)', data: top.map(p => p.avg_latency_ms || 0), backgroundColor: '#39ff9c' }]
  };
  if (chart) { chart.data = chartData; chart.update(); }
  else {
    chart = new Chart(ctx, { type: 'bar', data: chartData, options: { plugins: { legend: { labels: { color: '#dce8e2' } } },
      scales: { x: { ticks: { color: '#6f8a7d' }, grid: { color: '#1c2b25' } }, y: { ticks: { color: '#6f8a7d' }, grid: { color: '#1c2b25' } } } } });
  }
}

loadStats();
load();
setInterval(() => { loadStats(); load(); }, 15000);
</script>
</body>
</html>
"""
