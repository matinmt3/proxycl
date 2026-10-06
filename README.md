# Revmamad — Telegram MTProto Proxy Radar

A toolkit that **discovers, probes, benchmarks, scores, and exports** Telegram MTProto proxies. It sends an obfuscated transport probe and measures responses as a heuristic health check. Runs on a PC or directly on Android via Termux.

---

## Why this is more than a bash-script proxy checker

| | Typical scripts | This project |
|---|---|---|
| Validation | TCP connect only | Real obfuscated2 MTProto handshake + abridged-framing probe |
| Concurrency | Sequential / xargs | `asyncio` with a configurable worker pool (100 / 500 / 1000+) |
| Scoring | Sort by ping | Weighted score: latency, success rate, stability, timeout rate |
| Sources | One list | Pluggable multi-source collector (GitHub, JSON feeds, TXT feeds, local files) |
| Output | One text file | JSON, TXT, CSV, `tg://` links, top10/50/100 |
| Operations | Run once | Manual / continuous / interval-based scheduler modes |
| Visibility | None | FastAPI dashboard with charts, search, sort, filter, JSON API |
| Packaging | Script | Dockerfile + docker-compose, one-command startup |

---

## Project layout

```
TelegramProxySelector/
├── collector/       # multi-source proxy discovery + parsing + de-dup
├── tester/          # real MTProto obfuscated-handshake validator + parallel runner
├── benchmark/       # weighted scoring algorithm
├── exporter/        # json/txt/csv/tg-links/topN exporters
├── dashboard/        # FastAPI web dashboard
├── scheduler/        # manual / continuous / interval scheduler
├── utils/            # shared models, colored logger, progress bar
├── config/            # config.yaml + loader + example manual proxy list
├── tests/             # pytest suite
├── docs/              # extended documentation
├── main.py            # CLI entrypoint
├── install.sh         # Termux installer + automatic menu launcher
├── requirements.txt
├── requirements-lite.txt
├── Dockerfile
├── docker-compose.yml
└── LICENSE
```

---

## Installation

```bash
git clone https://github.com/matinmt3/proxycl.git Revmamad
cd Revmamad
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Requires Python 3.12+.

Two dependency files are provided:

| File | What it installs | Use it for |
|---|---|---|
| `requirements.txt` | everything: CLI + web dashboard + test/lint tools | PC, Docker |
| `requirements-lite.txt` | only what the command-line features need | Termux / phones, quick installs |

The web dashboard is optional. With the lite file, add it later with `pip install fastapi uvicorn`.

### One-command install (Termux)

After the source is published on the `main` branch with `install.sh` at the
repository root, install and launch Revmamad in Termux with this single line:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/matinmt3/proxycl/main/install.sh)
```

Use `bash <( ... )` so the interactive menu keeps its keyboard input. If `curl`
is missing, first run `pkg install -y curl`. The installer:

1. Updates the Termux package index and installs `python`, `python-pip`, `git`,
   `clang`, `make`, and `pkg-config`.
2. Clones the `main` branch into `~/Revmamad`, regardless of your current directory.
3. Installs `requirements-lite.txt` with `python -m pip`.
4. Automatically starts `python main.py` and opens the existing menu.

Running it again updates the same checkout with a fast-forward pull. It stops
with a visible error if that directory belongs to another repository, uses a
different branch, contains tracked local edits, or if installation/update fails.
The installer supports both a repository with app files at its root and one
with the app in a `TelegramProxySelector/` subdirectory.

Next time, for the root layout, run:

```bash
cd ~/Revmamad && python main.py
```

Using a fork? Point the installer at it without editing anything:

```bash
REVMAMAD_REPO=https://github.com/<you>/<repo>.git bash <(curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/install.sh)
```

Optional overrides are `REVMAMAD_DIR` (installation directory) and
`REVMAMAD_BRANCH` (default: `main`). If changing the branch, change the branch in
the raw download URL too. The installer prints the correct restart command for
the actual installation path, including the nested layout when present.

Termux manages pip through `python-pip`; do not upgrade pip with pip. See the
[Termux package patch](https://github.com/termux/termux-packages/blob/master/packages/python-pip/install_py_preventing_pip_from_installing.patch).

### Publishing a fork

Keep `install.sh` and the application files at the repository root on `main`.
To make a fork the installer's default source, change the default repository
URL in `install.sh` and update the clone/raw download links in this README.
Alternatively, use the `REVMAMAD_REPO` override shown above. Preserve the LF
line endings in shell scripts; `.gitattributes` already enforces them.
Confirm the raw `.../main/install.sh` link displays the script before sharing it.

### Running on Android via Termux (manual)

```bash
pkg update -y
pkg install -y python python-pip git clang make pkg-config
git clone https://github.com/matinmt3/proxycl.git Revmamad
cd Revmamad
python -m pip install -r requirements-lite.txt
python main.py
```

- `clang` is needed so `pycryptodome` can compile on-device.
- Keep package errors visible and resolve the specific missing package/build tool before retrying.
- The interactive menu works the same as on desktop. Option 1 gets you the 10 fastest working proxies.
- Dashboard on a phone: `python -m pip install fastapi uvicorn` (this may need `pkg install -y rust` because of pydantic), then `python main.py dashboard` and open `http://localhost:8000` in the phone's browser.

---

## Configuration

All behaviour is controlled by `config/config.yaml`:

```yaml
sources:
  - name: "local-manual-list"
    type: "http_txt"
    url: "file://config/manual_proxies.txt"
    enabled: true

testing:
  workers: 200
  timeout_seconds: 5.0
  retries: 3

scoring:
  latency_weight: 0.35
  success_rate_weight: 0.35
  stability_weight: 0.20
  timeout_weight: 0.10

output:
  folder: "output"
  min_score: 0.0
  top_sizes: [10, 50, 100]

scheduler:
  mode: "manual"       # manual | continuous | scheduler
  interval_minutes: 15

dashboard:
  host: "0.0.0.0"
  port: 8000
```

Add your own sources by appending to the `sources` list. Supported `type` values:

- `http_txt` — plain text, one `server:port:secret` per line, or `tg://proxy?...` links
- `http_json` / `json_feed` — JSON array or `{"proxies": [...]}` object
- `github_raw` — same parsing as `http_txt`/`http_json`, just semantically a GitHub raw URL
- any source URL beginning with `file://` is read from local disk (useful for manual/offline lists)

---

## Usage

```bash
python main.py                # interactive menu (easiest — just pick a number)
python main.py best10          # collect + test + show/save the 10 fastest working proxies
python main.py run             # full pipeline: collect + test + export (no menu)
python main.py collect         # collect only, print count
python main.py benchmark       # collect + test, print ranked table (no export)
python main.py export          # collect + test + export all formats
python main.py dashboard        # start the web dashboard on :8000
python main.py scheduler        # run in the configured scheduler mode
python main.py top 20           # print top 20 from the last export
python main.py json              # regenerate and print path to proxy.json
python main.py csv                # regenerate and print path to proxy.csv
```

Running `python main.py` with no arguments shows a simple numbered menu —
option **1** collects fresh proxies, tests them for real, and prints/saves
the 10 fastest currently-working ones with ready-to-tap `tg://` links
(also saved to `output/best10_links.txt`).

### Example output

```
RANK SERVER                PORT   SCORE   SUCCESS  AVG MS
------------------------------------------------------------
1    149.154.167.51        443    0.91    1.00     41
2    149.154.175.50        443    0.87    1.00     58
```

Exported files land in `output/`:

- `proxy.json`, `proxy.txt`, `proxy.csv`
- `telegram_links.txt` — ready-to-tap `tg://proxy?...` links
- `top10.json`, `top50.json`, `top100.json`

---

## How validation works

Real Telegram clients connect to MTProto proxies using an obfuscated handshake
(the same "obfuscated2" scheme documented in Telegram's own open-source
MTProxy code) before any application data flows. This project builds that
64-byte handshake using the proxy's secret, sends a minimal abridged-framed
probe, and requires a nonempty response within the timeout. An empty response,
connection error, or timeout is recorded as a failed attempt.

This can help filter unreachable endpoints and connections that immediately
close after the probe. The response is not decoded, so a positive result is a
heuristic and does not prove a valid secret or a usable Telegram session.
Completing a full DH key exchange with the Telegram datacenter behind the
proxy is outside the current implementation; see
[`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) for notes on extending this.

---

## Scoring

```
score = latency_weight   * (1 - avg_latency / max_latency)
      + success_weight   * success_rate
      + stability_weight * (1 - coefficient_of_variation)
      + timeout_weight   * (1 - timeout_rate)
```

Weights are normalized automatically if they don't sum to 1.0. Tune them in
`config.yaml` under `scoring:`.

---

## Dashboard

```bash
python main.py dashboard
# open http://localhost:8000
```

Shows total/healthy proxy counts, average latency, best/worst proxy, a
latency chart for the top proxies, and a searchable/sortable/filterable
table. Raw data is available at `GET /api/proxies` and `GET /api/stats`.

---

## Docker

```bash
docker compose up --build
```

This starts:
- `mtselector-dashboard` — dashboard on `http://localhost:8000`
- `mtselector-scheduler` — runs the pipeline in the scheduler mode set in `config.yaml`

`output/`, `logs/`, and `config/` are mounted as volumes so results and
config changes persist across container restarts.

---

## Troubleshooting

**All proxies show 0% success rate.**
Check that outbound TCP is actually allowed from your network/container to
arbitrary IPs on arbitrary ports — some sandboxed/corporate networks block
this. Also confirm your sources are returning real, currently-live proxies
(public lists rot quickly).

**A source keeps failing with an HTTP error.**
The collector logs a warning and continues with other sources — it will not
crash the whole run. Check the URL manually in a browser, or disable that
source (`enabled: false`) if it has gone offline.

**Dashboard shows no data.**
Run `python main.py export` at least once first — the dashboard reads
`output/proxy.json`.

---

## FAQ

**Does this decrypt or read my Telegram messages?**
No. It only performs the outer transport-layer handshake used to reach a
proxy; it never establishes an authorized Telegram session or touches
message content.

**Can I add private/paid proxy sources?**
Yes — add any `http_txt`/`http_json` source, or drop a `server:port:secret`
list at `config/manual_proxies.txt` and point a `file://` source at it.

**How many workers should I use?**
Start at 200 and watch your file-descriptor limits (`ulimit -n`) and network
conditions; 1000+ workers can work well on a server with a generous FD limit
and a fast NIC.

---

## Performance tuning

See [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md).

## License

MIT — see [`LICENSE`](LICENSE).
