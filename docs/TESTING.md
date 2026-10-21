# Version 3.3 validation

Validation host: Windows, Python 3.14. Dates and times below are UTC. Deterministic tests, local runtime checks, public-proxy observations, phone checks and publication are separate evidence.

## Integrated candidate checks

On **2026-10-21**, the integrated **v3.3.0 candidate passed 671 Python tests in 52.01 seconds and 19 Node dashboard-client tests**. Ruff passed, Black accepted all **87 Python files**, and Python compilation, JavaScript/Bash syntax and Git whitespace checks passed. The optional FastAPI test produced one third-party `StarletteDeprecationWarning`; it did not fail.

These are the release-wide totals. Focused runs and the native CLI interruption cases are included in that Python suite, not additional tests to add to its count. Protocol tests use controlled peers and fixtures; successful fixtures are not public working proxies.

| Area | Verified behavior |
| --- | --- |
| Staged Telegram scanning | Transport prefilter is separate from strict evidence; baseline ranking freezes a bounded finalist set; sustained sessions request the configured pongs and rotate DCs; worker counts stay bounded |
| Completed policy | Default completed staged scans require at least seven successful connections; eight successful connections supply 59 matched pongs; baseline-only candidates cannot bypass finalist testing, including with larger custom baseline budgets |
| Protocol / rejection | Raw/dd/ee framing, pinned RSA_PAD/DH exchange, MTProto 2.0 encryption, nonce/hash/message-key/auth-ID validation, matched pongs, session/salt/replay/time/sequence checks, containers and bounded key cleanup |
| Ctrl+C / native CLI | Finished samples and honest failure denominators survive cancellation; native CLI coverage includes both new staged interruption cases and existing mode/menu/scheduler flows; partial output is provisional and preserves the previous completed snapshot |
| Checkpoint / resume | Atomic snapshots preserve candidate order, frozen finalists, completed samples and DC rotation; stale, future, corrupt, mismatched and complete state is rejected; current config rebuilds single-stage MT policy; generic disk callbacks are serialized |
| Large Web checkpoints | SQLite actually round-tripped **100,001 candidates** in a deterministic test; transactional rollback, separate evidence rows, generation/configuration checks and storage failures are covered |
| Network / history / sources | Named network isolation, bounded history without double-counting a resumed run, fresh saved-candidate rechecks, first-source quality attribution, temporary failure cooldown and later retry |
| HTTP/SOCKS | HTTP CONNECT, TLS to HTTPS proxies, SOCKS4a/SOCKS5, origin certificate/status checks and no direct connection fallback |
| CLI / exports | ALL/custom candidate counts, mode isolation, input/EOF handling, strict/provisional labels, composite ranking and consistent eligible top-ten links across console/export formats |
| Dashboard | Strict proof/policy validation, stage and partial counters, history, safe terminal commands, qualified actions, legacy evidence and malformed-data handling; Node tests cover the client behavior |
| Installer / scheduler | Controlled installer command shims, retained executable/LF requirements, and scheduler loops stopping after an interrupted scan |

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python -m ruff check .
python -m black --check .
python -m compileall -q .
node --test tests/dashboard_client.test.cjs
node --check dashboard/dashboard.js
bash -n install.sh
git diff --check
```

## Installed-runtime selftest

The candidate's `python main.py selftest` passed **all eight checks** on the Windows/Python 3.14 host: three runtime dependencies, scan configuration, AES known-answer/IGE behavior, Telegram-link parsing, atomic checkpoint restoration and dashboard HTTP loopback. The dashboard check starts a real temporary server on `127.0.0.1` and fetches its HTML, JavaScript, CSS and snapshot route; it is not a mocked HTTP success.

The selftest uses temporary synthetic checkpoint data and opens only local loopback sockets. It launches no browser and contacts no Telegram or public proxy. Its success does not establish Termux package installation, Android browser-intent delivery, live Telegram connectivity or media download performance. The report records those scope flags explicitly.

## Current external and device status

- **Physical Android/Termux validation is pending.** The user will run the candidate on their phone using [TERMUX-CHECK.md](TERMUX-CHECK.md); no device pass is claimed before that result exists.
- **Current v3.3 visual/interactive browser validation is pending in this report.** The Python/Node and loopback checks above establish their stated coverage, not a responsive visual browser pass. Historical v3.1 DEMO viewport checks and the interrupted v3.2 browser attempt do not validate the v3.3 interface.
- Docker image/Compose execution has not been run on this host.

## Current bounded live staged sample

On **2026-10-21, 09:55:25.873–09:55:29.111**, a fresh catalog read returned **3,080 parser-valid unique MTProto candidates**. **50 of 51 remote feeds** yielded candidates; `V2RAYCONFIGSPOOL/TELEGRAM_PROXY_SUB/telegram_proxy_no1.txt` returned HTTP 404. The optional manual list was empty. Retrieval health tracking was disabled for this isolated observation; HTTP/SOCKS feeds were not scanned. The captured local report is `work/v3-3/research/source-collection.json` outside the product tree.

The first **256 candidates after deterministic source interleaving** received one transport diagnostic each; **33 passed**. The first eight positives formed a selected input pool for a new v3.3 staged test, recorded in `legacy-transport-selection.json`. From **09:56:57.622 to 09:59:58.382**, that test ran for **180.760 seconds including cleanup**, then its 180-second budget interrupted the finalist stage. It did **not** complete a normal production scan: the entire run and its recommendations remain **PROVISIONAL**.

| Recorded stage/evidence | Observed result |
| --- | --- |
| Staged input / transport screen | Eight selected candidates; all eight passed the repeated screen |
| Strict baseline | Eight candidates finished; seven qualified for the frozen finalist set |
| Finalist progress at interruption | Six of seven candidates finished all five extra attempts; 33 extra finalist attempts finished in total |
| Actual strict evidence | 57 finished connection attempts, 54 successful strict sessions and 379 matched encrypted pongs; three `handshake_timeout` failures |
| Eligible provisional results | Six eligible, two rejected; each eligible candidate had eight successful sessions, 59 matched pongs and verified DCs 2 and 4 |
| Eligible-candidate ping metrics | p95 RTT range **266.26–974.90 ms**; jitter range **20.49–178.57 ms** |

The actual samples and interrupted engine summary are in `work/v3-3/research/live-staged-sample.json` outside the product tree. Checkpoints, history and persistent source health were disabled for this bounded helper run. Cancelled in-flight work is not counted as a finished failure. The six candidates' recorded samples satisfy substantial session/ping coverage, but the run used the interrupted baseline policy and remains provisional globally. This is a small, selected Windows-network observation, not a simultaneous control comparison, a full-catalog health test, a phone measurement, an uptime guarantee or a media-download test. It does not establish that these endpoints will remain available later or on the user's internet connection.

## Historical source and protocol observations

These dated measurements belong to **v3.2 or earlier**. Public feed contents and routing change; none is a current source-health or phone-performance guarantee.

At **07:21:02 on 2026-10-21**, the v3.2 MTProto catalog returned **3,148 parser-valid unique candidates**: 664 raw, 296 dd and 2,188 ee. **50 of 51 remote feeds** yielded candidates; `V2RAYCONFIGSPOOL/TELEGRAM_PROXY_SUB/telegram_proxy_no1.txt` returned HTTP 404, and the optional manual list was empty. This was source-retrieval evidence, not endpoint testing. The captured local report is `source-collection.json`.

The v3.2 strict recheck captured at **07:32:06 on 2026-10-21** screened 256 public candidates with one nonce-matched transport probe each; **26 passed**. The first eight positive results in completion order received three fresh strict sessions each, rotating DC **2, 4, 2**, with three encrypted pongs per session. **Two of eight passed all three sessions and nine matched pongs**. Their measured p95 RTT/jitter pairs were **401.73/72.17 ms** and **207.32/9.71 ms**. The other six produced 18 attempts recorded as `handshake_timeout`. The captured local report is `live-session-recheck.json`.

That selected sequential sample verified production protocol interoperability; it was not a simultaneous comparison, a population false-positive estimate, a full-catalog scan or an uptime measurement. Three out of three successful connections have a 95% Wilson lower confidence bound of about **43.8%**. Those three-session results alone do not meet v3.3's completed staged minimum of seven successes. The earlier `live-strict-sample.json` predates the transport-padding correction and remains debugging evidence.

For earlier context, the **2026-10-08** catalog checks retrieved candidates from all 51 MTProto feeds and all 64 HTTP/SOCKS feeds; the Web collection contained 163,372 unique entries. A separate 31-candidate transport-only sample yielded one nonce-valid `resPQ` response. Neither proves strict Telegram sessions or current feed availability. The catalog includes multiple datasets from shared publishers, not 51 independent proxy operators; provenance and retrieval bounds are documented in [SOURCES.md](SOURCES.md).

Historical v3.2 release tests passed **503 Python / 13 Node** tests; v3.1 passed **364 / 8** and v3.0 passed **303 / 4**. These are historical totals, not additions to the current candidate count.

## Interruption and evidence boundaries

Ctrl+C retains only attempts that actually finished, including completed samples in an unfinished retry sequence. Cancellation does not create a failed attempt. Candidates without finished strict samples count as skipped; `fully_tested`/`partial_tested` describe completion of the configured baseline budget. Separate counters describe transport screens and finalist attempts.

Interrupted staged output uses the strict baseline gate and is labeled **PROVISIONAL**. It does not claim completion of sustained testing; two successful interrupted sessions still fail the minimum of three. Normal completion applies the stronger policy across every result. `snapshot.json` describes the current generation, while `completed_snapshot.json` preserves the previous full run during interruption. `observations.json` retains nonqualifying samples and separate prefilter outcomes without authorization keys. Only policy-qualified results populate usable link exports.

Resume requires the same mode, user-selected network label and configuration/source fingerprint. The default 30-minute lifetime includes run start and oldest evidence; an active scan may continue beyond that age while later resume is refused. Checkpoints are periodic, so abrupt process termination can lose work since the last committed checkpoint. History is bounded historical observation data and never supplies current eligibility. See [PERFORMANCE.md](PERFORMANCE.md) for precise limits and policies.

Strict Telegram sessions verify server identity and encrypted service replies without account login, authorized messaging or file downloads. Fresh MAIN keys remain in RAM; bounded cleanup is attempted, but failure can leave an unused server-side key. Default DC coverage is 2 and 4, not every media/home datacenter. HTTP/SOCKS success verifies the configured HTTPS origin through that proxy, not every website. Availability can differ on another internet connection or later.

## Publication

**v3.3 publication is pending at the time of this report.** Local tests, selftest and archive checks do not establish a public release. Separate publication verification must compare the public repository/tag and raw installer with the checked commit, then download the release assets and compare their sizes/SHA-256 values with the local artifacts. A later public verification report records that outcome independently.
