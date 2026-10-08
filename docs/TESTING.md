# Version 3 validation

Validation host: Windows, Python 3.14. Dates below are UTC. This record separates completed deterministic checks, live network samples and platform limits.

## Regression checks

The final v3 candidate passed **303 Python tests in 27.22 seconds** and **four Node dashboard-client tests** on 2026-10-08. Ruff, Black, Python compilation, JavaScript syntax, installer Bash syntax and Git whitespace checks passed. The optional FastAPI test emitted one third-party Starlette deprecation warning; it passed. The previous v2 baseline passed 172 tests; overlapping v3 component runs are not additional totals.

| Area | Covered behavior |
| --- | --- |
| Catalogs / collection | Independent explicit overrides including [], external config paths, validated Web defaults, TXT/JSON/URI/IPv6/secret parsing, deduplication, feed failures, safe ordered reports, 4 MiB bounds and eight concurrent reads |
| MTProto | Raw/dd/ee framing, encryption keys and stream counters, FakeTLS authentication, fragmented replies, nonce validation, malformed replies, timeouts and cleanup |
| Web transports | HTTP CONNECT, proxy-hop TLS, SOCKS4a and SOCKS5, origin TLS and response validation, malformed/fragmented replies, proxy/origin certificate rejection, no direct fallback |
| Workers | Bounded concurrency, retries, ordered results, mobile/file-descriptor caps, cancellation and socket cleanup |
| Scan / CLI | ALL and positive N, validation before collection, source interleaving after dedupe, independent modes, all menu choices, EOF/Ctrl+C, direct flags, help/version without runtime packages |
| Ranking / output | Composite-score order and tie breakers, verified/output filters, common console/readable/JSON top ten, completed empty scans, mode-isolated atomic snapshots, previous snapshot preserved on cancellation |
| Dashboard | Shared stdlib/FastAPI snapshot logic, mode/query validation, legacy MTProto fallback, safe links/text, source/count states, offline assets and client controls |
| Installer / scheduler | Controlled shell command shims for installer behavior; manual/continuous/interval scheduling and cancellation |

A separate network-mocked integration check used real CLI dispatch, scoring, scan and exports for both modes: 16 input rows containing one duplicate produced 15 tested candidates, 13 eligible and ten displayed. Console URI order matched top10.json, the snapshot and readable links. A reliable 101 ms endpoint outranked a 5 ms endpoint with a failed retry; high-latency and failed candidates were excluded.

To reproduce the regression and static checks:

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m ruff check .
python -m compileall -q .
bash -n install.sh
git diff --check
node --test tests/dashboard_client.test.cjs
node --check dashboard/dashboard.js
python -m black --check .
```

Browser checks used synthetic snapshots clearly marked **DEMO browser fixture**. At 320- and 390-pixel viewport widths the dashboard had no horizontal overflow. Mode switching, top-ten/25-result limits, search/no matches, protocol filtering, sorting, HTTP-80/HTTPS-443 URI copying, command copying, pause/manual refresh, empty completed scans and corrupt snapshots were checked. The page loaded only local CSS/JavaScript and produced no browser warning/error logs. A mobile screenshot is supplied separately; this check does not establish live proxy availability or physical Android behavior.

## Live source collection

| Check | Time on 2026-10-08 | Observed result |
| --- | --- | --- |
| MTProto catalog | 15:47:08–15:47:28 | 60 remote URLs configured; six GitHub feeds yielded 1,281 globally unique valid candidates. All 54 direct t.me/s channel requests returned host-specific ConnectError; the manual list was empty. |
| Web catalog | 15:55:05–15:55:11 | All 64 enabled feeds yielded parser-valid candidates: 162,627 globally unique endpoints, comprising 70,723 HTTP, 2,086 HTTPS-to-proxy, 22,369 SOCKS4 and 67,449 SOCKS5. |

Distinct URLs may share publishers or endpoints. Source counts are not counts of independently operated or working proxies. See [SOURCES.md](SOURCES.md) for provenance, protocol interpretation and availability limits.

## Live protocol samples

- **MTProto, 15:52:17:** 30 transport-balanced public candidates, one attempt each, produced **10 nonce-valid replies: nine raw and one dd**. No ee FakeTLS candidate passed this sample. The remaining attempts yielded ten TCP timeouts, eight invalid handshakes, one handshake timeout and one DNS error. The 1,281 collected candidates comprised 177 raw, 175 dd and 929 ee secrets; the whole collection was not health-tested.
- **Web, 15:55:11–15:55:26:** 20 candidates sampled across protocols, one attempt each, produced **one verified HTTP proxy**. The test required certificate-verified origin TLS and HTTP 204 from `https://www.gstatic.com/generate_204`, with proxy certificate validation enabled where applicable and no direct fallback. The remaining attempts yielded eight TCP timeouts, six invalid handshakes and five handshake timeouts. This sample establishes no live success for HTTPS-to-proxy, SOCKS4 or SOCKS5.
- **Web follow-up, 16:04:27:** after the CONNECT-2xx and persistent-204 fixes, the same 20-candidate sample produced no verified endpoint: six invalid handshakes, eight TCP timeouts and six handshake timeouts. The earlier successful public endpoint no longer passed. The deterministic local TLS fixtures passed both corrected protocol cases.

These small samples were executed on one Windows network. Endpoints, source contents and network restrictions can change; the result is not a promise about another phone, ISP or website.

## Release verification

Publication checks compare public repository bytes/file modes, the raw installer, release tag, downloaded ZIP and SHA256 checksum with the checked local artifacts. A release testing report records actual final test/static/browser results; successful local tests do not substitute for public asset verification.

## Platform limits

- Native Android/Termux package compilation and browser-intent delivery were not exercised on a physical phone. Android-specific behavior uses controlled tests and the official Termux interfaces.
- Docker image/Compose execution was not run on this Windows host.
- A nonce-matched unauthenticated resPQ response does not verify Telegram's RSA server identity or an authorized account/messaging session.
- A Web success verifies the configured HTTPS origin response at test time, not all websites. Local TLS fixture tests exercise all four Web protocols; the live sample passed only HTTP.
