# Version 2 validation

Validation date: 2026-10-06. Host: Windows, Python 3.14. **All 172 regression tests passed.** Ruff, Python compilation, Bash syntax, and Git whitespace checks also passed. The optional FastAPI test emits a dependency deprecation warning; it does not affect the standard-library phone dashboard.

| Area | Checked scenarios |
|---|---|
| Menu / CLI | All five menu choices; scan/export/collect/benchmark/top/json/csv/scheduler/dashboard commands; invalid choices, EOF, Ctrl+C, help/version, errors, custom config, paths outside the project |
| Config / models / logs | Invalid YAML and values, numeric bounds, source types, scoring weights, absolute paths, case-sensitive secrets, URL encoding, repeated logger setup |
| Collector | TXT/links/JSON/local sources, IPv6, base64 and hex secrets, deduplication, malformed rows, HTTP failures, feed size and concurrency limits |
| MTProto | Key derivation, AES stream continuity, raw/dd/ee transports, authenticated FakeTLS hello, fragmented responses, nonce validation, malformed/oversize responses, plain HTTP rejection, timeouts and socket cleanup |
| Workers | Bounded task counts, mobile/file-descriptor caps, retries, ordered results, cancellation and exception cleanup |
| Ranking / exports | Zero score for dead proxies, limit filtering, speed order, all file formats, IPv6 text, empty scans clearing stale data, atomic writes |
| Dashboard | Standard-library HTTP routes, optional FastAPI adapter, malformed/missing data, search/sort/stats, invalid queries, Android browser command; browser rendering/search/sort at a 390px viewport |
| Scheduler | Manual/continuous/interval modes, elapsed-time calculation, cancellation |
| Installer | 32 controlled shell cases: root/nested/worktree layouts, existing checkout refusal, incomplete clone cleanup/retry, path/branch edge cases, pip/package/clone/pull/import failures, keyboard input, application exit status |

The browser check used explicitly synthetic demo data. It confirmed that the page fits the phone viewport and the results table scrolls independently; it is not a screenshot of live proxy availability.

## Live network check

The Argh94 feed yielded 241 valid unique candidates (142 raw, 85 dd, 14 ee) at the time of testing. A mixed sample of 34 candidates, with one attempt each, produced **11 nonce-valid MTProto replies: 9 raw, 1 dd, and 1 ee FakeTLS**. This verifies interoperability beyond the local test servers. The remaining candidates failed with refused connections, timeouts, or invalid responses.

The two old Yagami feed URLs returned HTTP 404. The default config replaces them with the reachable SoliSpirit feed while retaining Argh94 and the optional local list. Malformed example secrets were removed from the default manual file.

A subsequent collection using the final default config returned 257 unique valid candidates (145 raw, 91 dd, 21 ee) from the two working remote feeds. These counts are snapshots and will change.

## Release checks

The full regression suite, Ruff, compilation, shell syntax, and Git whitespace checks run before publication. Public source bytes/modes, the installer URL, release tag, downloaded ZIP, and SHA256 checksum are checked after publication.

## Platform limits

- Native Android/Termux package compilation and actual Android browser-intent delivery were not executed on a physical phone. Android-specific behavior uses controlled tests and the official Termux package interfaces.
- Docker is not installed on the validation host; Docker image/Compose execution was not run.
- A nonce-matched unauthenticated resPQ is not a complete Telegram account login or verification of Telegram's RSA server identity. Public proxy availability and network restrictions can change after a scan.
