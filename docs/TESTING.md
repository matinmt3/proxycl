# Version 3.2 validation

Validation host: Windows, Python 3.14. Dates below are UTC. Source retrieval, protocol samples, deterministic tests and device validation are separate evidence.

## Regression checks

The final integrated **v3.2.0** candidate passed **503 Python tests in 35.97 seconds** and **13 Node dashboard-client tests** on **2026-10-21**. Ruff, Black, Python compilation, JavaScript/Bash syntax and Git whitespace checks passed. The optional FastAPI test passed with one third-party `StarletteDeprecationWarning`.

The dashboard's overlapping focused run passed **127 Python tests**; its 13 Node tests are the same client suite counted above. New failing tests were observed before implementation: old transport snapshots could be promoted without strict metadata, malformed policy/proof was accepted, and labels did not separate Telegram RTT from total probe duration. Corrected API/client gates require coherent counts, DC/ping coverage, configured RTT/jitter limits and Wilson arithmetic. Actual `ProxyResult.to_dict()` fixture data from three DC 2,4,2 connections round-tripped with nine pongs and the correct confidence bound. Focused counts are not additional release-wide tests.

The historical **v3.1.0** release passed **364 Python tests and eight Node client tests** on 2026-10-08; v3.0.0 passed 303 Python tests and four Node tests. Those historical counts do not validate v3.2's strict protocol changes and are not additional release-wide totals.

| Area | Covered behavior |
| --- | --- |
| Sources / config | Catalog overrides including [], local paths, safe ordered reports, feed isolation, size/concurrency bounds, recovery without blocked Telegram access, preserved HTTP/SOCKS catalog |
| MTProto / Telegram links | Raw/dd/ee framing, pinned-key RSA_PAD/DH exchange and encrypted ping validation; share links use actual MTProto endpoints/secrets, with three isolated output folders |
| Session rejection / cleanup | Unknown RSA keys, nonce/hash/DH failures, invalid message keys/auth-key IDs, mismatched pongs, session/salt/replay/time/sequence failures, container framing, bounded cleanup and cancellation |
| HTTP/SOCKS | HTTP CONNECT, proxy-hop TLS, SOCKS4a/SOCKS5, origin TLS/status checks, certificate rejection and no direct fallback |
| Interrupted runs | Finished samples retained during incomplete retries, unstarted attempts excluded, honest tested/full/partial/skipped counts, partial exports and preserved last-full snapshot |
| CLI / ranking | Six menu choices, three modes, ALL/N, invalid input/EOF/Ctrl+C, composite-score ranking and matching eligible top ten across console/export formats |
| Dashboard | Strict/transport distinction, policy validation, single-sample rejection, p95 RTT versus probe duration, safe qualified actions, legacy read-only evidence, three modes and interrupted counters |
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

Current **v3.2 browser validation did not complete**: the browser automation could not determine the current page URL and stopped before further UI input. The automated Python and Node dashboard tests passed, but no current 320/390-pixel visual or interactive browser pass is claimed. Historical v3.1 browser checks used **DEMO fixtures** at those widths, exercising copying, filters and stopped-scan counts; they predate the strict v3.2 interface and do not establish its live endpoint availability or physical Android behavior.

## Live source collection

At **07:21:02 on 2026-10-21**, the configured MTProto catalog returned **3,148 unique parser-valid candidates**: 664 raw, 296 dd and 2,188 ee. **50 of 51 remote feeds** yielded candidates; `V2RAYCONFIGSPOOL/TELEGRAM_PROXY_SUB/telegram_proxy_no1.txt` returned HTTP 404, and the optional manual list was empty. The collector isolated the failed feed. This is source-retrieval evidence, not a proxy-health result. The captured report is `source-collection.json` in the local validation evidence.

The following source checks belong to the earlier release:

| Check on 2026-10-08 | Time | Observed result |
| --- | --- | --- |
| Telegram-host access diagnostic | 18:39:54–18:40:10 | t.me and telegram.me resolved to a private IPv4 address; TCP 443 refused the connection before TLS/HTTP. GitHub raw files completed certificate-verified TLS and HTTP 200. No DNS or certificate settings were changed. |
| Replacement MTProto catalog | 18:46:30–18:46:34 | All 51/51 remote feeds yielded parser-valid candidates; all 51 normalized candidate sets differed. Global deduplication returned 2,303 candidates; manual list empty. |
| Preserved HTTP/SOCKS catalog | 18:48:26–18:48:41 | All 64/64 feeds yielded parser-valid candidates: 163,372 globally unique entries (70,821 HTTP, 2,101 HTTPS-to-proxy, 22,672 SOCKS4, 67,778 SOCKS5). |

The configured HTTP/SOCKS entries are byte-for-byte unchanged. MTProto feeds come from 16 repositories/15 publisher accounts, including published country/region datasets. They are not 51 independent proxy operators. Retrieval uses certificate verification, 4 MiB per-source limits and eight concurrent reads. See [SOURCES.md](SOURCES.md) for provenance.

## Live strict-session sample

The final captured recheck, **07:32:06 on 2026-10-21**, first tested **256 public candidates once each** with the old nonce-matched `req_pq_multi`/`resPQ` diagnostic; **26 passed** that prefilter. Candidates were interleaved across raw/dd/ee transports. The first eight positive results, in prefilter completion order, then received three fresh strict connections each, rotating DC **2, 4, 2**, with three encrypted pings per connection. This was a bounded validation script, not a full-catalog CLI scan.

**Two of those eight candidates passed all three strict connections and all nine matched pongs.** Their measured Telegram p95 RTTs were **401.73 ms** and **207.32 ms**, with jitter **72.17 ms** and **9.71 ms**, respectively. Both covered DC 2 and 4 and met the default 1,500 ms p95 / 500 ms jitter limits. Each had only three reconnect trials: an observed 100% pass rate has a 95% Wilson lower bound of about **43.8%**. The other six candidates had no successful strict connection in this recheck; all 18 failed attempts were recorded as `handshake_timeout`. The captured report is `live-session-recheck.json` in the local validation evidence.

This sequential, selected sample shows that transport responsiveness alone did not supply strict proof in this run. It is not a simultaneous head-to-head comparison, a population false-positive estimate, a complete health test of all 3,148 candidates, or a long-term availability measurement. The earlier `live-strict-sample.json` was collected before the transport-padding correction and is retained as debugging evidence, not the final benchmark. No account login, phone number, Telegram API credentials, messaging or download-throughput test was used.

## Historical transport sample — not strict verification

At **18:48:20 on 2026-10-08**, collection yielded **2,294 unique MTProto candidates** (702 raw, 346 dd, 1,246 ee). A sample plus the user's example tested **31 candidates, one attempt each**, using the old transport diagnostic. **One raw candidate returned a nonce-valid resPQ response**. The remaining attempts produced 22 TCP timeouts, six invalid handshakes, one refused TCP connection and one handshake timeout. This did not complete strict key exchange/encrypted-ping verification. The user's example was syntactically valid but did not pass that network probe.

Changing source contents explain why collection counts differ between timestamps. Syntax-valid candidates are not verified working proxies. The sample did not health-test the full catalog, log into Telegram or test a physical phone. Historical v3 HTTP/SOCKS samples were mixed: one HTTP success in an early 20-candidate sample, then zero in a follow-up. They are not current endpoint-health claims.

## Interrupted results and snapshots

Ctrl+C while probing keeps only attempts that actually finished, including finished samples of incomplete retry sequences. Success rates use those samples; cancellation does not fabricate a failed attempt. Candidates with no completed sample count as `skipped`; `tested` is the count with at least one finished sample. `fully_tested` and `partial_tested` compare recorded attempts with configured `retry_count`.

The current generation's `snapshot.json` sets `summary.interrupted: true` and contains qualifying results so far. Strict policy still applies: two successful reconnects before stopping are recorded but cannot populate the minimum-three top ten. Top-N/TXT/CSV use qualifying rows, and the readable file labels a stopped scan. `completed_snapshot.json` retains the last full generation. Normal completion updates both snapshots; interruption before probing leaves previous output intact. The menu resumes after displaying results; scheduler loops stop.

`observations.json` separately preserves all observed rows and finished samples, including failed or nonqualifying candidates. It carries the same scan summary and does not contain authorization keys. Observation rows are evidence to inspect; only policy-qualified rows populate usable Telegram link exports. Deterministic scan tests verify this distinction for both Telegram modes.

## Release and platform boundaries

Publication evidence is separate from this local validation report. Public release checks compare repository bytes/file modes, the raw installer, release tag and downloaded ZIP/checksum against the checked local artifacts. Passing local tests and a local release archive do not establish publication success; the release verification report records those checks separately.

- Physical Android/Termux package installation and Android browser-intent delivery were not executed. Controlled Android-interface tests and browser viewport checks do not substitute for a phone run.
- Docker image/Compose execution was not run on this host.
- The `transport` diagnostic does not verify Telegram's RSA identity. Strict `telegram_session` checks server identity and encrypted replies, without account login or authorized messaging. Fresh MAIN keys stay in RAM; bounded cleanup is attempted, but failure may leave an unused server-side key.
- HTTP/SOCKS success requires a verified TLS response from the configured HTTPS origin; it does not establish access to every website. Availability can change on another ISP or after the scan.
