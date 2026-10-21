# REVMAMAD v3.3 — Sustained Telegram Checks, Resume and Network History

Test Telegram server identity, encrypted ping replies and independent reconnects through MTProto proxies. The default scan screens candidates, runs strict sessions, then retests up to twenty finalists for longer before recommending up to ten. Export `tg://` or Telegram HTTPS share links; test HTTP/SOCKS internet proxies separately. Stop with Ctrl+C, retain provisional evidence and resume a recent matching scan. Network profiles keep results and history separate.

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
| 7 | Resume the last interrupted scan for the selected network and mode |
| 8 | Choose a local network profile name |
| 9 | View that network's observation history |
| 10 | Freshly recheck up to twenty saved qualified connections |

Each scan offers **1 ALL / 2 Custom count / 0 Back**. A custom N limits unique candidates after deduplication and source interleaving; it does not request N successful results. ALL selects the whole collected pool. Default Telegram scans run a transport prefilter on that pool and full strict tests on its passing candidates; `--no-prefilter` applies full strict tests to every selected candidate. Large HTTP/SOCKS collections can take a long time, so start with a small N on a phone. The terminal shows stage, counts, approximate remaining time and failed attempts. Fewer than ten results means fewer passed verification and output filters.

Strict Telegram checks cost more than a TCP connection or `resPQ` response. Start with a small candidate count; MTProto concurrency defaults to 16, independently of the HTTP/SOCKS worker setting.

```bash
python main.py best10 --mode mtproto --count ALL
python main.py best10 --mode telegram --count 100
python main.py best10 --mode web --count 100
python main.py best10 --mode mtproto --count 25 --network mobile
python main.py best10 --mode mtproto --resume --network mobile
python main.py recheck --mode mtproto --network mobile
python main.py history 10 --mode mtproto --network mobile
python main.py selftest --network mobile
python main.py best10 --mode mtproto --count 25 --no-prefilter
python main.py best10 --mode mtproto --count 25 --single-stage
python main.py collect --mode telegram
python main.py benchmark --mode web --count 25
python main.py dashboard --no-browser
python main.py top 20 --mode web
python main.py --config /path/to/custom.yaml best10 --count 50
python main.py --help
python main.py --version
```

Existing `run`, `best10`, `collect`, `benchmark`, `export`, `scheduler`, `top`, `json` and `csv` commands remain available. Test commands accept `--mode mtproto|telegram|web` and `--count ALL|N`; defaults are MTProto and ALL. The legacy `web` mode name means HTTP/SOCKS; `telegram` means Telegram HTTPS share links. `--single-stage` uses the v3.2 strict baseline without sustained finalists; `--no-prefilter` bypasses only the initial transport screen. Benchmark displays results without exports, checkpoints, history or persistent source-health updates. Invalid menu input retries and EOF exits. Operational errors return a nonzero status.

## Stop a scan and keep its results

Press **Ctrl+C during probing** to stop and show qualifying results so far. Finished samples remain in accounting; pending attempts are not invented as failures. Interrupted staged scans use the strict baseline gate and prominently label results **PROVISIONAL**: sustained finalist testing did not finish. Two successful reconnects out of two interrupted attempts still fail the minimum of three. A normally completed staged scan applies the stronger finalist policy; baseline-only candidates cannot enter its top ten. The menu returns after showing results, a direct stopped-scan command returns success after saving them, and scheduled runs stop their loop.

The saved summary marks `interrupted: true` and records stage/provisional status, `selected`, `tested` (at least one finished strict attempt), `skipped`, `fully_tested`, `partial_tested` and retry budgets. Prefilter outcomes have separate counters. The current `snapshot.json` shows this partial run; `completed_snapshot.json` retains the last full run. A normal completed scan updates both. Ctrl+C during collection, before probing begins, leaves the previous snapshot intact.

## Resume, profiles and fresh rechecks

Use `best10 --resume --mode MODE --network NAME`, or menu 7, to continue the saved candidate pool and finished attempts. Omit `--count` on resume; supplying a different count is rejected. The mode, network, source/configuration fingerprint and scan strategy must match. Repeat any original `--single-stage` or `--no-prefilter` override. Completed, corrupt, future-dated or stale checkpoints are rejected rather than presented as current measurements.

The default checkpoint lifetime is **30 minutes**, including the run's start time and oldest saved evidence; a recently written file can still be too old to resume. An active ALL scan can keep running and saving beyond that age. Configure `scan.checkpoint_max_age_minutes` up to 1,440 if needed. Small scans use atomic JSON; Web scans above 20,000 selected candidates use transactional SQLite, with a one-million-candidate safety cap. Checkpoints are throttled to one second, or five seconds for large scans; Ctrl+C forces a final flush. An abrupt process/Android termination can lose work since the last committed checkpoint.

`--network wifi` and `--network mobile` are **names you choose**, not automatic Wi-Fi/carrier detection. The default profile writes `output/MODE/`; a named profile writes `output/networks/NAME/MODE/`. Change the label yourself when changing internet connections. Checkpoints, source health and history are isolated by profile/mode; MTProto and Telegram-share-link discovery share MTProto source health.

`history` shows recorded observations with last-test time and past success counts; history never qualifies a current recommendation. Each run records at most 1,000 ranked observations, retaining at most 1,000 runs and 100,000 historical observations. `recheck` loads up to twenty already-qualified saved candidates for that network/mode and runs fresh tests. It does not reuse historical scores or account login; if no qualified saved candidates exist, run a fresh scan first.

## Mobile dashboard

Menu 4 uses Python's standard-library server; the lightweight installation includes everything it needs. Open the printed address, normally **http://127.0.0.1:8000**, if the browser does not open automatically. Keep that terminal session running; Ctrl+C stops the server.

The dashboard has separate MTProto, Telegram Web Link and HTTP/SOCKS tabs, live stage/progress, provisional labels, source-quality counts, network history, filtering and copyable qualifying links. Strict cards show **Telegram RTT p95**, session passes as **n/N**, matched pong/DC counts and a labeled reliability lower bound. The separate **Probe duration** includes setup time. Old transport-only results are read-only evidence and request a new strict scan; malformed strict metadata cannot enable connection actions. Refresh/pause controls manage dashboard updates; polling pauses while hidden. The dashboard is read-only: it provides commands to copy, while scans, resume and recheck run in the terminal. Assets are local, with no CDN dependency. Use `dashboard --network NAME` to view the intended profile.

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
scan:
  strategy: staged
  prefilter: true
  finalists: 20
  finalist_retries: 5
  finalist_ping_count: 10
  finalist_ping_interval_seconds: 1
  finalist_reconnect_delay_seconds: 2
  finalist_workers: 4
  network: default
  resume: false
  checkpoint_enabled: true
  checkpoint_max_age_minutes: 30
  history_enabled: true
  source_health_enabled: true
  source_failure_threshold: 3
  source_cooldown_minutes: 30
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

Three consecutive network/HTTP fetch errors temporarily cool down a remote source for 30 minutes; it is retried automatically after expiry. Empty feeds, parser/size errors and local manual files do not trigger quarantine. Successful/empty fetches reset the failure streak. Source-health metadata stores URL digests, fetch counts/status and tested/qualified counts, without feed credentials. Deduplicated candidates are credited only to their first known source; ambiguous source names on resume are not given invented attribution. Health-file IO failures warn without stopping collection. The catalog is unchanged; its 51/64 feed counts are not a promise that every source is currently active.

## Verification, ranking and output

**MTProto / Telegram Web Link:** both modes use strict `telegram_session` verification. They complete Telegram's RSA_PAD/DH authorization-key exchange against pinned Telegram RSA keys, then require matched encrypted MTProto 2.0 ping/pong replies. The default staged scan runs a transport screen, a baseline of three independent connections with three pings per connection, then five additional fresh connections for up to twenty eligible finalists. Finalist connections request ten pongs at one-second intervals with a two-second reconnect delay, using at most four finalist workers. This takes roughly a minute per successful finalist, plus setup/network delays; a whole scan with several batches takes longer.

Normally completed staged scans require at least **seven successful strict connections by default**, an observed pass rate of at least 80%, both configured DCs (2 and 4), p95 RTT at most 1,500 ms and jitter at most 500 ms. The minimum is raised according to the configured baseline/finalist budgets and pass rate; nonfinalists with baseline-only evidence are excluded. Interrupted results are provisional under the baseline minimum of three. `--single-stage` deliberately uses that baseline policy without the sustained stage. `min_score: 0` cannot bypass any policy. See [protocol and metric definitions](docs/PERFORMANCE.md) for the exact rules.

This establishes server-authenticated protocol responsiveness at test time. It does not log into an account, send messages or measure file-download throughput. No phone number, login code or Telegram API credentials are needed. A fresh MAIN authorization key stays in RAM; bounded `destroy_auth_key` cleanup is attempted. Failed cleanup can leave an unused server-side key with no account association. A `https://t.me/proxy?...` link is an MTProto share link, not an HTTP proxy. See [protocol and metric definitions](docs/PERFORMANCE.md).

`testing.mtproto_profile: transport` retains the old nonce-matched `resPQ` diagnostic. It does not satisfy strict verification or produce usable Telegram link exports. Legacy snapshots are never silently upgraded to strict results.

**HTTP/SOCKS (`web`):** HTTP CONNECT, certificate-verified HTTPS-to-proxy, SOCKS4a or SOCKS5 must reach `https://www.gstatic.com/generate_204` through the proxy, negotiate certificate-verified TLS to that origin and receive HTTP 204. There is no direct connection fallback. Success measures reachability to that origin at scan time. An `https` filename often describes HTTP CONNECT support; an explicit `https://` proxy URI means TLS to the proxy. Certificate checks remain enabled.

All modes use a common ranking order across console, readable links and `top10.json`. Strict Telegram scores use reconnect confidence, encrypted-ping p95 RTT and ping jitter; ties use RTT and endpoint key. Even eight successful connections are a short, correlated sample, not proof of long-term uptime. HTTP/SOCKS retains its existing scoring. Failed or nonqualifying candidates are excluded. Account-authorized media/download testing remains a future feature.

Files are isolated by mode and network as described above:

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
| `progress.json` | Read-only current stage/progress and resumability metadata |
| `history.json` | Bounded historical observations for the selected network/mode |

Private runtime state lives under `output/.state/`; it contains public proxy settings and finished evidence, never Telegram authorization keys or feed credentials. A network-level `selftest-report.json` records offline runtime/loopback diagnostics separately from proxy measurements.

A completed empty scan clears its mode's current exports. Files are replaced atomically; the dashboard reads counts and rows from a single snapshot generation. Scheduler modes are manual, continuous and interval.

## Validation

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m ruff check .
```

Run `python main.py selftest` on the installed device for dependency, AES/runtime, parser, atomic checkpoint and real local-dashboard HTTP checks. It needs no pytest, account login or external proxy connection; it does not test Android browser intent or live Telegram connectivity. Follow the [Termux device check](docs/TERMUX-CHECK.md) for those separate manual flows. See [validation evidence](docs/TESTING.md) for actual release checks and dated live observations; local deterministic tests do not establish phone or current public-proxy performance.

## License

MIT — see [LICENSE](LICENSE).
