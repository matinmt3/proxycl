# Version 3.1 validation

Validation host: Windows, Python 3.14. Dates below are UTC. Source retrieval, protocol samples, deterministic tests and device validation are separate evidence.

## Regression checks

The final **v3.1.0** candidate passed **364 Python tests in 28.84 seconds** and **eight Node dashboard-client tests** on 2026-10-08. Ruff, Black, Python compilation, JavaScript/Bash syntax and Git whitespace checks passed. The optional FastAPI test emitted one third-party `StarletteDeprecationWarning`; it passed.

The historical v3.0.0 release passed 303 Python tests and four Node client tests; these are not v3.1 totals. The overlapping v3.1 source/config/collector run passed 79 tests. Its consumer regression first failed against the 54 blocked Telegram defaults, then passed with captured samples from every replacement feed.

| Area | Covered behavior |
| --- | --- |
| Sources / config | Catalog overrides including [], local paths, safe ordered reports, feed isolation, size/concurrency bounds, recovery without blocked Telegram access, preserved HTTP/SOCKS catalog |
| MTProto / Telegram links | Real raw/dd/ee transport checks, nonce validation, Telegram HTTPS share links derived from actual MTProto endpoints/secrets, three isolated output folders |
| HTTP/SOCKS | HTTP CONNECT, proxy-hop TLS, SOCKS4a/SOCKS5, origin TLS/status checks, certificate rejection and no direct fallback |
| Interrupted runs | Finished samples retained during incomplete retries, unstarted attempts excluded, honest tested/full/partial/skipped counts, partial exports and preserved last-full snapshot |
| CLI / ranking | Six menu choices, three modes, ALL/N, invalid input/EOF/Ctrl+C, composite-score ranking and matching eligible top ten across console/export formats |
| Dashboard | Shared stdlib/FastAPI data logic, three mode labels, Telegram share-link actions, interrupted-run counters, safe links/text, offline assets and empty/stale/corrupt states |
| Installer / scheduler | Controlled installer command shims; scheduler loops stop after an interrupted scan |

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m ruff check .
python -m black --check .
python -m compileall -q .
bash -n install.sh
git diff --check
node --test tests/dashboard_client.test.cjs
node --check dashboard/dashboard.js
```

Browser checks used synthetic snapshots explicitly marked **DEMO fixture**. The dashboard had no horizontal overflow at 320- and 390-pixel widths. Native MTProto URI copying, Telegram HTTPS share-link copying, HTTP/HTTPS URI copying, command clipboard actions, search/clear filters, the 25-result limit, Pause/resume updates and manual Refresh were exercised. An interrupted fixture correctly displayed 239 candidates with samples of 1,000 selected and 761 skipped. Browser warning/error logs were empty. These checks do not establish live endpoint availability or physical Android behavior.

## Live source collection

| Check on 2026-10-08 | Time | Observed result |
| --- | --- | --- |
| Telegram-host access diagnostic | 18:39:54–18:40:10 | t.me and telegram.me resolved to a private IPv4 address; TCP 443 refused the connection before TLS/HTTP. GitHub raw files completed certificate-verified TLS and HTTP 200. No DNS or certificate settings were changed. |
| Replacement MTProto catalog | 18:46:30–18:46:34 | All 51/51 remote feeds yielded parser-valid candidates; all 51 normalized candidate sets differed. Global deduplication returned 2,303 candidates; manual list empty. |
| Preserved HTTP/SOCKS catalog | 18:48:26–18:48:41 | All 64/64 feeds yielded parser-valid candidates: 163,372 globally unique entries (70,821 HTTP, 2,101 HTTPS-to-proxy, 22,672 SOCKS4, 67,778 SOCKS5). |

The configured HTTP/SOCKS entries are byte-for-byte unchanged. MTProto feeds come from 16 repositories/15 publisher accounts, including published country/region datasets. They are not 51 independent proxy operators. Retrieval uses certificate verification, 4 MiB per-source limits and eight concurrent reads. See [SOURCES.md](SOURCES.md) for provenance.

## Live protocol sample

At **18:48:20 on 2026-10-08**, a later collection yielded **2,294 unique MTProto candidates** (702 raw, 346 dd, 1,246 ee). A small transport-balanced sample plus the user's example tested **31 candidates, one attempt each**. **One raw candidate returned a nonce-valid MTProto response**. The remaining attempts produced 22 TCP timeouts, six invalid handshakes, one refused TCP connection and one handshake timeout. No dd/ee candidate passed this sample. The user's example was syntactically valid but did not pass the network probe.

Changing source contents explain why collection counts differ between timestamps. Syntax-valid candidates are not verified working proxies. The sample did not health-test the full catalog, log into Telegram or test a physical phone. Historical v3 HTTP/SOCKS samples were mixed: one HTTP success in an early 20-candidate sample, then zero in a follow-up. They are not current endpoint-health claims.

## Interrupted results and snapshots

Ctrl+C while probing keeps only attempts that actually finished, including finished samples of incomplete retry sequences. Success rates use those samples; cancellation does not fabricate a failed attempt. Candidates with no completed sample count as `skipped`; `tested` is the count with at least one finished sample. `fully_tested` and `partial_tested` compare recorded attempts with configured `retry_count`.

The current generation's `snapshot.json` sets `summary.interrupted: true` and contains eligible results so far. Normal top-N/TXT/CSV exports use those same rows; interrupted status belongs to the snapshot summary, not a field invented in every result. The readable best-ten file labels a stopped scan. `completed_snapshot.json` retains the last full generation. A normal completed scan updates both snapshots; interruption during collection before probing leaves previous output intact. The menu shows saved partial results and resumes; direct partial commands return success after export, while scheduler loops stop.

## Release and platform boundaries

Public verification compares repository bytes/file modes, the raw installer, release tag and downloaded ZIP/checksum against checked local artifacts. A separate release report accompanies these checks; local passing tests alone do not establish publication success.

- Physical Android/Termux package installation and Android browser-intent delivery were not executed. Controlled Android-interface tests and browser viewport checks do not substitute for a phone run.
- Docker image/Compose execution was not run on this host.
- An unauthenticated nonce-valid resPQ response does not verify Telegram's RSA identity or an authorized messaging session. Telegram share-link mode uses that same MTProto probe.
- HTTP/SOCKS success requires a verified TLS response from the configured HTTPS origin; it does not establish access to every website. Availability can change on another ISP or after the scan.
