# Telegram protocol and measurement notes

## Staged verification in version 3.3

Both MTProto and Telegram HTTPS-share-link modes use `testing.mtproto_profile: telegram_session`. After raw, dd or ee transport setup, each strict connection completes RSA_PAD/DH key creation against pinned Telegram RSA keys and validates encrypted MTProto 2.0 ping replies. See Telegram's [key-creation protocol](https://core.telegram.org/mtproto/auth_key), [encrypted-message format](https://core.telegram.org/mtproto/description) and [service messages](https://core.telegram.org/mtproto/service_messages).

The default `scan.strategy: staged` has three separate stages:

| Stage | Default work | Role |
| --- | --- | --- |
| Prefilter | One nonce-matched transport probe per selected candidate | Cheap screening; its reply is never strict evidence |
| Baseline | Three independent strict connections, DC 2/4 rotation, three pongs per connection at 0.5-second intervals | Find candidates eligible for finalist selection |
| Finalists | Up to twenty baseline-qualified candidates; five additional fresh strict connections, ten pongs per connection at one-second intervals, two-second reconnect delay | Collect longer observations before final recommendations |

Finalist concurrency is at most four, also respecting the configured MTProto/overall worker and platform caps. Ten pings have nine spacing intervals; five successful finalist connections plus reconnect delays take roughly a minute per finalist before variable setup, RTT and cleanup costs. Multiple batches and timeouts make the total scan longer. This is a short sustained probe, not an uptime guarantee.

A normally completed staged scan raises the required strict-success count to:

```text
max(output.mtproto_min_successes,
    testing.retries + 1,
    ceil(output.mtproto_min_success_rate *
         (testing.retries + scan.finalist_retries)))
```

With the defaults, that is **seven successful strict connections** out of up to eight attempts. The policy applies to every result; a candidate outside the finalist set cannot qualify from its three baseline connections alone. DC coverage, measured pass rate and RTT/jitter limits remain mandatory. The saved `strict_policy` records the actual minimum applied to each result.

| Requirement | Default completed staged scan |
| --- | --- |
| Successful independent strict connections | At least 7 |
| Observed connection pass rate | At least 80% |
| Verified DC coverage | 2 and 4 |
| Pongs requested per baseline/finalist connection | 3 / 10 |
| Telegram ping p95 RTT | At most 1,500 ms |
| Telegram ping jitter | At most 500 ms |

On Ctrl+C, finished strict observations use the baseline gate of at least three successful connections and are marked **PROVISIONAL**. This lets users inspect evidence so far without claiming that the finalist phase completed. Two out of two interrupted successes still fail that gate. Resuming continues missing work; it does not invent failures for cancelled attempts.

`--single-stage` explicitly chooses the v3.2 strict baseline without sustained finalists. `--no-prefilter` keeps strict/finalist stages but applies them to all selected candidates instead of excluding failed screens. A screen can fail under transient conditions; bypassing it trades more work for a fresh strict attempt on every candidate. The explicit `transport` profile remains diagnostic and cannot produce usable Telegram recommendations. `output.min_score: 0` never bypasses protocol or policy requirements.

## Scope and account boundaries

This is server-authenticated protocol testing without account authorization. No phone number, login code or API ID/hash is required; it does not open an authorized messaging session, send messages or download a file. Each fresh MAIN authorization key stays in RAM. The verifier attempts bounded `destroy_auth_key` cleanup before closing; if cleanup fails, the server may retain an unused key with no account association. Generated session keys are not export fields.

Service pings do not test every home/media datacenter or later network conditions. Default DC coverage is 2 and 4. Account-authorized file/download testing remains future work. A Telegram `https://t.me/proxy?...` link is a share link for an MTProto endpoint, not an HTTP internet proxy.

HTTP/SOCKS (`web`) remains a separate single-stage probe: HTTP CONNECT, certificate-verified HTTPS-to-proxy, SOCKS4a or SOCKS5 must reach `https://www.gstatic.com/generate_204`, verify origin TLS and receive HTTP 204 through that exact proxy. There is no direct connection fallback. This measures reachability to that origin at the time of testing.

## Read the metrics correctly

| Export field | Meaning |
| --- | --- |
| `verification_profile` | `telegram_session` for strict checks, `transport` for the old diagnostic, `web` for HTTP/SOCKS |
| `strict_policy`, `strict_eligible`, `strict_successes` | Actual qualification policy, its result and strict-success count |
| `attempts`, `successes`, `success_rate` | Finished attempts, their successes and observed rate; cancellation invents no attempts |
| `dc_ids_verified`, `ping_count` | Distinct DCs and matched pongs from successful strict samples |
| `telegram_median_ms`, `telegram_p95_ms` | Median and nearest-rank p95 of encrypted-ping round trips |
| `telegram_jitter_ms` | Mean absolute difference between consecutive successful ping RTTs, including across connections |
| `reliability_lower_bound` | 95% Wilson lower bound, using reconnect attempts rather than correlated pings |
| `avg_latency_ms` | Mean successful full-probe duration, including setup; a diagnostic metric |
| `stability` | Strict profile: clamped ping-jitter consistency; HTTP/SOCKS: existing successful-duration consistency |

The primary strict UI latency is **Telegram RTT p95**. No Mbps/MB/s value is inferred from handshake or ping duration. Three successes from three connections have a Wilson lower bound of about 43.8%; eight from eight have about 67.6%. Trials can share network conditions, so even the larger short sample does not establish long-term availability.

Strict scores use reconnect confidence, p95 RTT and ping jitter, with common ordering across console, exports and dashboard. The old `transport` profile checks a nonce-matched `req_pq_multi`/`resPQ`; it does not produce usable Telegram link exports. Old snapshots are labeled transport evidence and need a new strict scan. Dashboard policy/metric validation rejects inconsistent qualification claims; a saved JSON report is not itself a cryptographic attestation.

## Resume and persistence boundaries

`--resume` restores only a matching mode/network/configuration and its saved candidate order/finished samples. It does not recollect a different pool. Configuration fingerprints include scan semantics and source identities; a changed strategy, source list or testing policy requires a fresh scan. A backend pointer ties the checkpoint to its run generation so an old JSON/SQLite alternative is not silently selected.

The default age limit is 30 minutes. Validation includes `created_at`, run context/start time and the oldest saved sample, not just the most recent file modification. An active ALL scan can continue saving and finish after that limit, but later resume can be refused as stale. The configured maximum is 1,440 minutes. Future timestamps, corrupt rows, duplicate candidates/results and complete checkpoints are rejected on resume; restored rankings are recomputed from validated samples.

Small checkpoints use bounded atomic JSON (100,000 candidates / 64 MiB). Web pools above 20,000 selected candidates use SQLite transactions, separate candidate/result rows and updates only for changed rows. SQLite caps candidates at one million, individual metadata/evidence rows at 2 MiB and its database at 4 GiB. These limits do not promise phone RAM or scan speed: candidate/result lists still exist in memory. Start with a small candidate count on a phone; default MTProto strict concurrency is 16, finalist concurrency four, and HTTP/SOCKS workers have separate platform caps. Connect, authorization, ping and cleanup waits are bounded.

Periodic checkpoints are throttled to one second, or five seconds for large pools. Ctrl+C forces a final completed-evidence flush. Abrupt process termination or Android background eviction can lose work after the last committed checkpoint. Disk/write failures are reported; a scan with an unavailable checkpoint cannot promise resume.

## Network history, source health and UI

Network labels such as `wifi` or `mobile` are user-selected local namespaces, without automatic carrier detection. Default exports live at `output/MODE/`; named profiles use `output/networks/NAME/MODE/`. Source health and checkpoints live under the output's `.state/` namespace. MTProto and Telegram-share-link modes share MTProto source discovery health.

History records at most 1,000 ranked observations per run, with at most 1,000 runs and 100,000 historical observations. Counts and last-test times describe stored observations, never current eligibility. `recheck` selects up to twenty saved qualified candidates and runs fresh tests; saved candidate JSON has a 4 MiB read bound. Empty/unqualified saved pools need a fresh scan.

Three consecutive remote HTTP/network fetch failures cause a 30-minute cooldown, followed by automatic retry. Empty feeds reset the streak; parser/size failures and manual local files do not quarantine a source. Tested/qualified counts credit only the first deduplicated source; resume restores attribution only for an unambiguous stored source name. A valid, nonempty feed still does not prove its endpoints work. Source-health IO failures warn without stopping collection.

The dashboard is read-only. It reads progress, results, source quality and bounded history, and provides terminal commands to copy. Refresh/pause affects polling, not the scan process; there are no dashboard POST controls to start or stop probes. Current interrupted snapshots preserve the previous full `completed_snapshot.json`.

The installed-device `selftest` checks imports, AES/IGE runtime behavior, parsing, atomic checkpoint restoration and actual loopback dashboard HTTP routes. It contacts no Telegram/public proxy, launches no browser and claims no Android intent validation. Follow [TERMUX-CHECK.md](TERMUX-CHECK.md) for those manual checks. Actual deterministic, live-network and device evidence belongs separately in [TESTING.md](TESTING.md).

Additional references: [Telegram transports](https://core.telegram.org/mtproto/mtproto-transports), official TDLib [TLS initialization](https://github.com/tdlib/td/blob/master/td/mtproto/TlsInit.cpp) and [TCP transport](https://github.com/tdlib/td/blob/master/td/mtproto/TcpTransport.cpp).
