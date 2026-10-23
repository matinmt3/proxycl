# Version 3.4 validation

Validation date: **2026-10-23**. Host: Windows, Python **3.14.6**. Times in live evidence are UTC. Local deterministic, public network, browser, physical-device and publication evidence are distinct. Previous release evidence is preserved in [the v3.3 record](archive/TESTING-v3.3.md).

## Integrated release candidate

The complete v3.4 candidate passed **939 Python tests in 101.37 seconds** and **25 Node dashboard-client tests**. Ruff passed, Black accepted all **109 Python files**, and Python compilation, JavaScript/Bash syntax and Git whitespace checks passed. One third-party optional FastAPI/Starlette deprecation warning remains; no tests failed or skipped.

These are suite totals, not sums of repeated focused runs. Fixtures do not establish public proxy availability.

| Area | Checked behavior |
| --- | --- |
| Telegram WEB contract | Canonical IDNA/base paths and secret markers; ambiguity, malformed query, unknown fields and unsupported inner secrets rejected; four carrier profiles, bootstrap/WELCOME, frame/cursor/window/retry bounds |
| WEB proof boundary | Raw/dd nonce-only peers can screen positive yet fail strict identity proof; requested DC and continuous obfuscated stream are preserved; no direct TCP fallback; cancellation closes the carrier |
| Existing Telegram proof | Pinned RSA_PAD/DH, MTProto 2.0 encrypted pongs, session/salt/replay/hash/time/sequence checks and bounded key cleanup; HTTP/site/WSS alone cannot qualify links |
| Time and local pressure | Bounded tasks and adaptive admission; local loop lag rather than remote failures controls pressure; absolute deadline includes collection/setup/checkpoints across all engines; no fabricated unfinished attempts |
| Staged timeboxes | Finalist reserve, provisional coverage, preserved complete candidate pool, frozen unfinished finalists and repeated Resume without replaying finished sustained samples |
| Smart Resume | Fresh sample retention and expired-candidate reset, seven-day original-pool bound, stale screening reset, JSON/large SQLite state, configuration guards and idempotent history |
| Four-mode integration | Local WEB Markdown/JSON collection, 59-pong sustained fixture, export eligibility, checkpoint/history/recheck roundtrips and independent mode/network paths |
| HTTP/SOCKS | HTTP CONNECT, HTTPS-to-proxy, SOCKS4a/SOCKS5 and verified origin TLS/status; no direct fallback |
| Local controls | Actual callback dispatch, start/stop/resume/recheck and failure states, single flight, bounded body, token/Host/Origin/peer guards, cross-site rejection and read-only compatibility |
| CLI/installer/scheduler | ALL/custom/time/menu/input/EOF paths, import/dedup/back, partial output and native interruption regressions, installer shims/executable LF and stopped scheduler behavior |

```bash
python -m pip install -r requirements.txt
python -m pytest -q
node --test tests/dashboard_client.test.cjs
python -m ruff check .
python -m black --check .
python -m compileall -q collector config dashboard exporter scheduler telegram_webproxy tester utils webproxy main.py scan.py
node --check dashboard/dashboard.js
bash -n install.sh
git diff --check
```

## Installed runtime and native CLI

An actual `python main.py selftest --network v34-agent-check` passed all **11 offline checks** on this host: four dependencies, configuration, AES/IGE, MTProxy parsing, pinned WEB HMAC/path-marker vectors, atomic checkpoint roundtrip, actual dashboard asset/snapshot HTTP and authenticated local start/stop. Wrong token/Origin rejection and failure cleanup have regression coverage. The report explicitly marks physical Android workflows untested.

A separate isolated subprocess harness passed **77/77 native CLI checks**, using local empty sources and documentation-only manual links. It exercised all four modes, named-network paths, collection, run/best10/benchmark, saved exports/history, expected missing/completed Resume and recheck rejections, time/fixed-worker arguments, import dedup/invalid/back, menu routes and a real read-only local dashboard. Its server subprocess was terminated and waited for within a bound; that cleanup is not a physical Android Ctrl+C claim. These 77 checks are separate from pytest, while the subprocess regression tests included in pytest remain part of its 939 total.

## Actual browser interactions

The real standard-library dashboard and actual control class ran on localhost with clearly labeled **DEMO** saved data and a deterministic synthetic worker. Browser clicks exercised custom 25/mobile start and stop, Scan Resume completion, Recheck completion, WEB three-minute start/stop, WEB ALL start/stop and explicit Recheck Resume completion. Recorded callbacks confirm the selected mode/network/count and 180-second versus unlimited policy. WEB HTTPS clipboard content was read back; direct `tg://webproxy` attributes were inspected without launching Telegram.

All four tabs were checked at **320 and 390 CSS pixels**. Document client/scroll widths were **305/305 and 375/375** respectively, including scrollbar space: no horizontal overflow. Console error/warning capture was empty. Desktop/mobile screenshots and event logs are retained outside the product in `work/v3-4/final-qa/`. These browser fixtures establish interaction/rendering, not live proof or Android intent delivery.

## Official reference-server interoperability

All **eight cases** (four carrier modes times root/base path) echoed **5,120,000 bytes per case**, crossing the 4 MiB flow-control window, through the pinned official Go relay auth/wire handlers. The isolated Windows test build overlaid only Unix file-permission checks; its cleartext loopback front and echo backend do not establish public TLS/Telegram or physical-device behavior. The overlays and executable are not included in the release. Full details and primary links are in [TELEGRAM-WEB.md](TELEGRAM-WEB.md).

## Bounded public observations

The public WEB operator README fetched successfully and yielded one deduplicated candidate after correcting inline-Markdown extraction. Its default native transport screen timed out after **9.35 seconds**, with no exported result. A separate strict native attempt at **08:48:00–08:48:15 UTC** timed out after **15.26 seconds**, before the bridge GET returned.

The host resolver returned `10.10.34.35` and a matching unusual IPv6 address for that hostname, while Cloudflare DoH through the configured HTTP environment proxy returned a different public IPv4. A direct origin HTTPS diagnostic also timed out before bootstrap. This identifies a resolver/network discrepancy; it cannot establish that the operator is inactive or validate public carrier interoperability. Certificates were never bypassed and the product does not change system DNS or detour its carrier through an environment proxy.

Small control collections retrieved **402 unique MTProto candidates from 3/3 feeds** and **7,549 HTTP/HTTPS/SOCKS4 candidates from 3/3 feeds**. In the verified run, **15/16** native MTProto transport probes passed and **two single-session DC 2 strict controls** each authenticated and returned three matched pongs. Those are individual protocol controls, not completed sustained qualification or top-ten recommendations. Eight probes sampled only HTTP internet proxies and yielded zero successes; an earlier run had one, illustrating short-term variance. Evidence is retained in `work/v3-4/final-qa/live/independent-native-observation-verified.json`; no account login or media download occurred.

The final full MTProto catalog refresh found one removed repository (HTTP 404); replacing it with the actually retrieved shablin publisher feed restored **51/51 nonempty remote feeds**, **2,462 unique candidates**, in **2.607 seconds**. The replacement supplied 309 parsed candidates that already overlapped the combined pool. Public fixture samples preserve per-entry capture provenance. Feed success does not qualify any endpoint.

A test-only host-specific resolver override for the WEB hostname preserved the original URL/SNI, default certificate validation and direct native route. It still failed before the bridge GET completed; a supplementary IP-to-TLS diagnostic timed out after 6.083 seconds. These diagnostics did not change product/system DNS or establish public carrier success.

## Device and release limits

- ADB is installed, but `adb devices -l` returned no connected devices. **Physical Android/Termux installation, actual Telegram WEB client behavior and Android browser intent have not been run.** Use the separate [Termux checklist](TERMUX-CHECK.md).
- Native Python/OpenSSL differs from Telegram's embedded WebView TLS fingerprint and full retry/lifecycle behavior. Protocol interoperability is not a guarantee of identical DPI resistance or application/media performance.
- Docker/Compose execution was not run. Account-authorized media/download tests remain future work.
- The release ZIP is built only from committed tracked product files plus Git history, preserving prior release artifacts. CRC, per-entry SHA-256, extracted Git tree/clean status, installer LF/executable mode and the extracted test suites are checked separately. Public commit/tree/tag/installer and all three downloaded asset hashes are verified after publication; publication verification files remain external evidence rather than claims from a local test.
