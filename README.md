# REVMAMAD v3.2 — Strict Telegram Checks + HTTP/SOCKS

Test Telegram server identity, encrypted ping replies and independent reconnects through MTProto proxies. Export qualifying endpoints as `tg://` or Telegram HTTPS share links; test HTTP/SOCKS internet proxies separately. Choose ALL or a candidate count, then see up to ten eligible results. Ctrl+C retains finished samples, without promoting incomplete evidence to a usable link.

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
| 1 | Strict MTProto checks; show and save up to ten eligible `tg://` links |
| 2 | Scan Telegram Web Link; export `https://t.me/proxy?...` links |
| 3 | Scan HTTP/SOCKS internet proxies |
| 4 | Open the local dashboard |
| 5 | Collect candidates; choose any of the three groups |
| 6 | Exit |

Each scan offers **1 ALL / 2 Custom count / 0 Back**. A custom N limits unique candidates to test after deduplication and source interleaving; it does not request N successful results. ALL tests every collected candidate. Large HTTP/SOCKS collections can take a long time, so start with a small N on a phone. The terminal shows counts, progress and failed attempts. Fewer than ten results means fewer passed the probe and output filters.

Strict Telegram checks cost more than a TCP connection or `resPQ` response. Start with a small candidate count; MTProto concurrency defaults to 16, independently of the HTTP/SOCKS worker setting.

```bash
python main.py best10 --mode mtproto --count ALL
python main.py best10 --mode telegram --count 100
python main.py best10 --mode web --count 100
python main.py collect --mode telegram
python main.py benchmark --mode web --count 25
python main.py dashboard --no-browser
python main.py top 20 --mode web
python main.py --config /path/to/custom.yaml best10 --count 50
python main.py --help
python main.py --version
```

Existing `run`, `best10`, `collect`, `benchmark`, `export`, `scheduler`, `top`, `json` and `csv` commands remain available. Test commands accept `--mode mtproto|telegram|web` and `--count ALL|N`; defaults are MTProto and ALL. The legacy `web` mode name means HTTP/SOCKS; `telegram` means Telegram HTTPS share links. Benchmark displays results without exporting. Invalid menu input retries and EOF exits. Operational errors return a nonzero status.

## Stop a scan and keep its results

Press **Ctrl+C during probing** to stop and show qualifying results so far. Finished samples remain in accounting; pending attempts are not invented as failures. Strict requirements still apply: two successful reconnects out of two interrupted attempts do not qualify under the minimum of three. The menu returns after showing results, a direct stopped-scan command returns success after saving them, and scheduled runs stop their loop.

The saved summary marks `interrupted: true` and records `selected`, `tested` (at least one finished attempt), `skipped` (no finished attempts), `fully_tested`, `partial_tested` and the configured `retry_count`. The current `snapshot.json` shows this partial run; `completed_snapshot.json` retains the last full run. A normal completed scan updates both. Ctrl+C during collection, before probing begins, leaves the previous snapshot intact. Benchmark still exports nothing.

## Mobile dashboard

Menu 4 uses Python's standard-library server; the lightweight installation includes everything it needs. Open the printed address, normally **http://127.0.0.1:8000**, if the browser does not open automatically. Keep that terminal session running; Ctrl+C stops the server.

The dashboard has separate MTProto, Telegram Web Link and HTTP/SOCKS tabs, scan counts, interrupted-run labels, source health, filtering and copyable qualifying links. Strict cards show **Telegram RTT p95**, session passes as **n/N**, matched pong/DC counts and a labeled reliability lower bound. The separate **Probe duration** includes setup time. Old transport-only results are read-only evidence and request a new strict scan; malformed strict metadata cannot enable connection actions. Refresh/pause controls manage updates; polling pauses while hidden. Assets are local, with no CDN dependency. Scans start in the terminal.

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

The catalog ships **51 enabled remote MTProto feeds**, one optional manual list, and **64 enabled HTTP/SOCKS feeds**. MTProto and Telegram-link modes share the same MTProto sources and tests. An HTTP/SOCKS endpoint is never converted into a fabricated Telegram secret. Publishers and endpoints can overlap; more feeds do not guarantee more working proxies. See [source provenance and dated availability](docs/SOURCES.md).

Edit `config/config.yaml` or use `--config`. The built-in config loads `source_catalog: config/sources.yaml`. An explicit `sources` or `web_sources` list replaces only that mode's catalog list, including `[]`. Older external configs use only their explicit sources. Relative paths resolve from the project root for `config/config.yaml`, or from an external config's containing directory.

```yaml
source_catalog: config/sources.yaml
sources: []                   # disable MTProto/Telegram discovery; keep HTTP/SOCKS
testing:
  workers: 50
  mtproto_profile: telegram_session
  mtproto_workers: 16
  auth_timeout_seconds: 12
  ping_timeout_seconds: 3
  ping_count: 3
  ping_interval_seconds: 0.5
  telegram_dc_ids: [2, 4]
  timeout_seconds: 5
  connect_timeout_seconds: 3
  retries: 3
output:
  folder: output
  min_score: 0
  max_latency_ms: 5000
  top_sizes: [10, 50, 100]
  mtproto_min_successes: 3
  mtproto_min_success_rate: 0.8
  mtproto_max_ping_p95_ms: 1500
  mtproto_max_ping_jitter_ms: 500
```

Feeds support text, JSON and local `file://` lists. MTProto also supports public Telegram channel previews and `tg://proxy`/`https://t.me/proxy` links. Failed feeds are isolated and reported; both collectors enforce 4 MiB per source and eight concurrent reads. Testing workers and timeouts are bounded. Public Web feed entries with credentials are excluded.

## Verification, ranking and output

**MTProto / Telegram Web Link:** both modes use strict `telegram_session` verification by default. They complete Telegram's RSA_PAD/DH authorization-key exchange against pinned Telegram RSA keys, then require matched encrypted MTProto 2.0 ping/pong replies. Defaults are three independent reconnects, rotating DC 2, 4, 2, with three pings per connection spaced 0.5 seconds apart. Qualification requires at least three successful reconnects, an observed pass rate of at least 80%, both configured DCs, p95 RTT at most 1,500 ms and jitter at most 500 ms. `min_score: 0` cannot bypass these checks.

This establishes server-authenticated protocol responsiveness at test time. It does not log into an account, send messages or measure file-download throughput. No phone number, login code or Telegram API credentials are needed. A fresh MAIN authorization key stays in RAM; bounded `destroy_auth_key` cleanup is attempted. Failed cleanup can leave an unused server-side key with no account association. A `https://t.me/proxy?...` link is an MTProto share link, not an HTTP proxy. See [protocol and metric definitions](docs/PERFORMANCE.md).

`testing.mtproto_profile: transport` retains the old nonce-matched `resPQ` diagnostic. It does not satisfy strict verification or produce usable Telegram link exports. Legacy snapshots are never silently upgraded to strict results.

**HTTP/SOCKS (`web`):** HTTP CONNECT, certificate-verified HTTPS-to-proxy, SOCKS4a or SOCKS5 must reach `https://www.gstatic.com/generate_204` through the proxy, negotiate certificate-verified TLS to that origin and receive HTTP 204. There is no direct connection fallback. Success measures reachability to that origin at scan time. An `https` filename often describes HTTP CONNECT support; an explicit `https://` proxy URI means TLS to the proxy. Certificate checks remain enabled.

All modes use a common ranking order across console, readable links and `top10.json`. Strict Telegram scores use reconnect confidence, encrypted-ping p95 RTT and ping jitter; ties use RTT and endpoint key. Three out of three passes are a small sample, with a 95% Wilson lower bound of about 43.8%, not proof of 100% uptime. HTTP/SOCKS retains its existing scoring. Failed or nonqualifying candidates are excluded.

Files are isolated in **`output/mtproto/`**, **`output/telegram/`** and **`output/web/`**:

| File | Contents |
| --- | --- |
| `proxy.json`, `proxy.txt`, `proxy.csv` | Ranked eligible results |
| `telegram_links.txt` | `tg://` links in MTProto mode; `https://t.me/proxy` links in Telegram mode |
| `proxy_links.txt` | HTTP/SOCKS connection URIs in Web mode |
| `best10_links.txt` | Up to ten eligible results with metrics and links |
| `top10.json`, `top50.json`, `top100.json` | Top-N subsets; top10 is always generated |
| `snapshot.json` | Latest full or interrupted scan's summary, source reports and eligible rows |
| `completed_snapshot.json` | Last normally completed scan, retained during interrupted runs |
| `observations.json` | All completed attempt evidence, including failed and insufficient strict results; no session keys |

A completed empty scan clears its mode's current exports. Files are replaced atomically; the dashboard reads counts and rows from a single snapshot generation. Scheduler modes are manual, continuous and interval.

## Validation

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m ruff check .
```

The **2026-10-08** source check fetched 51/51 MTProto feeds (2,303 syntax-valid candidates) and 64/64 HTTP/SOCKS feeds (163,372 candidates). A later 31-candidate MTProto sample produced one raw **transport-diagnostic** reply. Those historical checks did not run v3.2 strict verification; source availability and proxy health can change. See [validation evidence](docs/TESTING.md) for current test results and explicitly dated live checks.

See [validation evidence](docs/TESTING.md) for test coverage and release checks. Native Android/Termux installation and Docker execution have not been validated on this host.

## License

MIT — see [LICENSE](LICENSE).
