# REVMAMAD v2 — Telegram MTProto Proxy Radar

Collect, verify, rank, and export Telegram MTProto proxies. Version 2 includes a phone-sized ASCII **REVMAMAD** banner and a web dashboard that works with the lightweight Termux installation.

## Install or update in Termux

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/matinmt3/proxycl/main/install.sh)
```

If curl is missing, run `pkg install -y curl` first. The installer installs Python, pip, Git, clang, make, and pkg-config, clones into `~/Revmamad`, installs `requirements-lite.txt`, checks runtime dependencies, and opens the menu automatically. Use process substitution as shown so the menu retains keyboard input.

Run the same command to update. Errors remain visible; pip uses bounded connection timeouts and retries. Interrupted fresh clones are cleaned up. Existing tracked edits, another repository, or another branch stop the update rather than overwrite your files.

Next time:

```bash
cd ~/Revmamad && python main.py
```

Overrides: `REVMAMAD_DIR`, `REVMAMAD_REPO`, and `REVMAMAD_BRANCH`. A branch override also requires changing the branch in the raw download URL. Root and nested `TelegramProxySelector/` layouts are supported; the installer prints the correct restart command.

Termux manages pip through `python-pip`; do not upgrade pip with pip. See the [official package patch](https://github.com/termux/termux-packages/blob/master/packages/python-pip/install_py_preventing_pip_from_installing.patch).

## Menu and commands

| Menu | Action |
|---|---|
| 1 | Scan and display **up to 10 verified** proxies, sorted by latency; save links |
| 2 | Full scan and export JSON, TXT, CSV, links, and top-N files |
| 3 | Start the local web dashboard and open the browser |
| 4 | Collect candidates without claiming they are working |
| 5 | Exit |

`1 / 10` means one candidate passed the checks on your network. Ten is a maximum, not a guaranteed supply of live proxies. The scan reports collected, tested, verified, and failure counts. It never fills the list with failed candidates.

```bash
python main.py --help
python main.py --version
python main.py best10
python main.py run
python main.py collect
python main.py benchmark
python main.py export
python main.py dashboard
python main.py dashboard --no-browser
python main.py scheduler
python main.py top 20
python main.py json
python main.py csv
python main.py --config /path/to/custom.yaml best10
```

Invalid input returns to the menu. Ctrl+C cancels a scan. CLI errors produce a clear message and nonzero status. Default config, local lists, exports, and logs resolve from the project directory even when launched elsewhere. Relative paths in a custom YAML resolve from its containing directory.

## Phone web dashboard

Menu option 3 and `python main.py dashboard` use Python's standard library; **FastAPI, uvicorn, and Rust are not required**. The command invokes `termux-open-url` on Android and a regular browser on desktop. If automatic opening is unavailable, open the printed address, normally **http://127.0.0.1:8000**.

Keep the dashboard's Termux session running while viewing it. Ctrl+C stops it and returns to the menu. Results appear after a scan/export. Search, sort, statistics, and links remain usable when the optional chart CDN is unavailable. The dashboard uses your configured output folder and handles missing or malformed output files.

Desktop/container deployments can use the optional FastAPI adapter:

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
python -m pip install -r requirements.txt
python main.py
```

`requirements-lite.txt` contains runtime packages. `requirements.txt` also includes optional desktop dashboard and development/test tools.

## Configuration and sources

Edit `config/config.yaml` or pass `--config`. Worker counts, timeouts, scoring weights, scheduler modes, and source types are validated before a scan.

Sources support text, `tg://proxy` and `https://t.me/proxy` links, JSON arrays/objects, and local `file://` lists. Malformed entries and failed feeds are skipped so other sources continue. Duplicate candidates are combined. Public feeds and proxies change independently of this program; sources can be edited or disabled.

```yaml
sources:
  - name: manual
    type: http_txt
    url: file://config/manual_proxies.txt
    enabled: true
testing:
  workers: 100
  timeout_seconds: 5
  connect_timeout_seconds: 3
  retries: 3
output:
  folder: output
  min_score: 0
  max_latency_ms: 5000
  top_sizes: [10, 50, 100]
scheduler:
  mode: manual
  interval_minutes: 15
```

Supported feed types: `http_txt`, `github_raw`, `http_json`, `json_feed`.

## Verification and output

Version 2 derives encryption keys from the secret, retains AES stream state, sends an unauthenticated MTProto `req_pq_multi`, and checks the matching nonce in `resPQ`. Random bytes and ordinary website responses do not count as verified proxies. Raw, `dd` padded-intermediate, and `ee` FakeTLS secrets use distinct transports.

A successful check proves the endpoint relayed that probe at that time. It does not log into Telegram, create an authorized session, read messages, or guarantee later connectivity. See [protocol/performance notes](docs/PERFORMANCE.md).

Exports contain only successful candidates satisfying score and latency limits. Empty scans replace stale exports. Atomic file replacement prevents the dashboard reading half-written JSON.

| File | Contents |
|---|---|
| `proxy.json`, `proxy.txt`, `proxy.csv` | Ranked results |
| `telegram_links.txt` | Telegram connection links |
| `best10_links.txt` | Up to ten fastest eligible results |
| `top10.json`, `top50.json`, `top100.json` | Configurable top-N subsets |

Completely unsuccessful proxies score zero. Scheduler modes are manual, continuous, and interval; Ctrl+C stops the loop. Docker Compose runs dashboard and scheduler services with output/config/log volumes.

## Tests

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m ruff check .
```

The suite covers collection/parsing, transport validation, workers, scoring, exports, menu/CLI actions, configuration, dashboard routes/browser opening, scheduling, and the installer. Installer tests use controlled command shims rather than modifying host packages. See [v2 validation](docs/TESTING.md) for checked scenarios and platform limits.

## License

MIT — see [LICENSE](LICENSE).
