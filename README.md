# REVMAMAD v3 — MTProto + Web Proxy Radar

Collect, verify, rank and export Telegram MTProto and Web proxies independently. Choose ALL or a candidate count, then see up to ten verified eligible results in the terminal and a local mobile dashboard.

## Install or update in Termux

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/matinmt3/proxycl/main/install.sh)
```

If curl is missing, run `pkg install -y curl` first. The installer installs Python, pip, Git and build tools, clones into `~/Revmamad`, installs `requirements-lite.txt`, checks dependencies and opens the menu. Process substitution keeps keyboard input available.

Run the same command to update. Existing tracked edits, another repository or another branch stop the update so your work is preserved. Next time:

```bash
cd ~/Revmamad && python main.py
```

Overrides: `REVMAMAD_DIR`, `REVMAMAD_REPO` and `REVMAMAD_BRANCH`. A branch override also requires the same branch in the raw installer URL. Root and nested `TelegramProxySelector/` layouts are supported. Termux manages pip through `python-pip`; see its [official package patch](https://github.com/termux/termux-packages/blob/master/packages/python-pip/install_py_preventing_pip_from_installing.patch).

## Menu and commands

| Menu | Action |
| --- | --- |
| 1 | Scan MTProto; show and save up to ten verified eligible results |
| 2 | Scan Web proxies; show and save up to ten verified eligible results |
| 3 | Open the local dashboard |
| 4 | Collect candidates; choose MTProto or Web |
| 5 | Exit |

Each scan offers **1 ALL / 2 Custom count / 0 Back**. A custom N limits unique candidates to test after deduplication and source interleaving; it does not request N successful results. ALL tests every collected candidate. Large Web collections can take a long time, so start with a small N on a phone. The terminal shows selected/tested/verified/eligible counts, progress and failed attempts. Fewer than ten results means fewer passed the probe and output filters.

```bash
python main.py best10 --mode mtproto --count ALL
python main.py best10 --mode web --count 100
python main.py collect --mode web
python main.py benchmark --mode web --count 25
python main.py dashboard --no-browser
python main.py top 20 --mode web
python main.py --config /path/to/custom.yaml best10 --count 50
python main.py --help
python main.py --version
```

Existing `run`, `best10`, `collect`, `benchmark`, `export`, `scheduler`, `top`, `json` and `csv` commands remain available. Test commands accept `--mode mtproto|web` and `--count ALL|N`; defaults are MTProto and ALL. Benchmark displays results without exporting. Invalid menu input retries, EOF exits, and Ctrl+C cancels a scan and returns to the menu. Direct command errors return a nonzero status.

## Mobile dashboard

Menu 3 uses Python's standard-library server; the lightweight installation includes everything it needs. Open the printed address, normally **http://127.0.0.1:8000**, if the browser does not open automatically. Keep that terminal session running; Ctrl+C stops the server.

The dashboard has separate MTProto/Web tabs, completed-scan time and counts, source health, failure details, search, sorting, Web protocol filtering and copyable connection links. Refresh/pause controls manage updates; polling pauses while the page is hidden. CSS, JavaScript and fonts are local or system-provided, so the interface has no CDN dependency. It handles no scan, empty results, stale data, invalid snapshots and filters with no matches. Scans start in the terminal.

Optional desktop/container adapter:

```bash
uvicorn dashboard.app:app --host 0.0.0.0 --port 8000
```

## Desktop installation

Python 3.12 or newer:

```bash
git clone https://github.com/matinmt3/proxycl.git Revmamad
cd Revmamad
python -m venv .venv
# Linux/macOS: source .venv/bin/activate
# Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-lite.txt
python main.py
```

`requirements.txt` additionally installs the optional FastAPI adapter and development/test tools.

## Sources and configuration

The catalog ships **60 enabled remote MTProto URLs**, one optional manual list, and **64 enabled Web feed URLs**. URLs are distinct; publishers and endpoints can overlap. More feeds do not guarantee more working proxies. See [source provenance and dated availability](docs/SOURCES.md).

Edit `config/config.yaml` or use `--config`. The built-in config loads `source_catalog: config/sources.yaml`. An explicit `sources` or `web_sources` list replaces only that mode's catalog list, including `[]`. Older external configs use only their explicit sources. Relative paths resolve from the project root for `config/config.yaml`, or from an external config's containing directory.

```yaml
source_catalog: config/sources.yaml
sources: []                   # disable MTProto; keep Web catalog
testing:
  workers: 50
  timeout_seconds: 5
  connect_timeout_seconds: 3
  retries: 3
output:
  folder: output
  min_score: 0
  max_latency_ms: 5000
  top_sizes: [10, 50, 100]
```

Feeds support text, JSON and local `file://` lists. MTProto also supports public Telegram channel previews and `tg://proxy`/`https://t.me/proxy` links. Failed feeds are isolated and reported; both collectors enforce 4 MiB per source and eight concurrent reads. Testing workers and timeouts are bounded. Public Web feed entries with credentials are excluded.

## Verification, ranking and output

**MTProto:** raw, `dd` padded-intermediate and `ee` FakeTLS transports require a nonce-matched `req_pq_multi`/`resPQ` response. This is an unauthenticated transport check; it does not log into Telegram or verify an authorized messaging session. See [protocol notes](docs/PERFORMANCE.md).

**Web:** HTTP CONNECT, certificate-verified HTTPS-to-proxy, SOCKS4a or SOCKS5 must reach `https://www.gstatic.com/generate_204` through the proxy, negotiate certificate-verified TLS to that origin and receive HTTP 204. There is no direct connection fallback. Success measures reachability to that origin at scan time. An `https` filename often describes HTTP CONNECT support; an explicit `https://` proxy URI means TLS to the proxy. Certificate checks remain enabled.

Both modes rank by **composite score descending**, then latency ascending and endpoint key. Reliability, latency, stability and timeouts contribute to the score. Console, readable links and `top10.json` use the same eligible top ten. Failed candidates and those outside score/latency limits are excluded.

Files are isolated in **`output/mtproto/`** and **`output/web/`**:

| File | Contents |
| --- | --- |
| `proxy.json`, `proxy.txt`, `proxy.csv` | Ranked eligible results |
| `telegram_links.txt` / `proxy_links.txt` | MTProto / Web connection links |
| `best10_links.txt` | Up to ten eligible results with metrics and links |
| `top10.json`, `top50.json`, `top100.json` | Top-N subsets; top10 is always generated |
| `snapshot.json` | One completed scan's summary, source reports and eligible rows |

A completed empty scan clears its mode's current exports. Cancellation preserves the previous completed snapshot. Files are replaced atomically; the dashboard reads counts and rows from a single snapshot generation. Scheduler modes are manual, continuous and interval.

## Validation

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m ruff check .
```

On 2026-10-08, six GitHub MTProto feeds produced **1,281 unique valid candidates**; all 54 direct Telegram channel requests failed from the Windows validation host. A 30-candidate MTProto sample produced **10 valid replies: nine raw and one dd**. The 64 Web feeds produced **162,627 unique valid candidates**; a 20-candidate sample produced **one verified HTTP proxy**. Candidate totals are parsing results, not working-proxy counts. These are dated network samples, not availability guarantees.

See [validation evidence](docs/TESTING.md) for test coverage and release checks. Native Android/Termux installation and Docker execution have not been validated on this host.

## License

MIT — see [LICENSE](LICENSE).
