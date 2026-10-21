# Telegram protocol and measurement notes

## What a strict check establishes

Version 3.2 defaults to `testing.mtproto_profile: telegram_session` for both MTProto and Telegram HTTPS-share-link modes. After raw, dd or ee transport setup, it completes the modern RSA_PAD/DH key exchange against pinned Telegram RSA keys and validates encrypted MTProto 2.0 ping replies. A matching initial `resPQ` alone is insufficient. See Telegram's [key-creation protocol](https://core.telegram.org/mtproto/auth_key) and [encrypted-message format](https://core.telegram.org/mtproto/description).

Defaults are three independent connections, rotating DC 2, 4, 2, and three matched ping/pong exchanges per connection with a 0.5-second interval. All three successful connections and at least nine matched pongs are therefore needed. The saved `strict_policy` records configured requirements:

| Requirement | Default |
| --- | --- |
| Successful independent connections | At least 3 |
| Observed connection pass rate | At least 80% |
| Verified DC coverage | 2 and 4 |
| Matched pongs per successful connection | At least 3 |
| Telegram ping p95 RTT | At most 1,500 ms |
| Telegram ping jitter | At most 500 ms |

Reducing `output.min_score` does not bypass these requirements. A fast endpoint with one successful reply does not become a usable strict result. If Ctrl+C stops after two successful reconnects, those samples count in the stopped scan but do not qualify for its top ten. Normal and interrupted scans apply the same policy.

This is server-authenticated protocol testing without account authorization. No phone number, login code or API ID/hash is required; it does not open an authorized messaging session, send messages or download a file. Each fresh MAIN authorization key stays in RAM. The verifier attempts bounded `destroy_auth_key` cleanup before closing; if cleanup fails, the server may retain an unused key with no account association. Generated session keys are not export fields.

## Read the metrics correctly

| Export field | Meaning |
| --- | --- |
| `verification_profile` | `telegram_session` for strict checks, `transport` for the old diagnostic, `web` for HTTP/SOCKS |
| `strict_eligible`, `strict_successes` | Whether policy passed, and count of successful strict reconnect samples |
| `attempts`, `successes`, `success_rate` | Finished attempts, their successes and observed rate; cancellation invents no attempts |
| `dc_ids_verified`, `ping_count` | Distinct DCs and matched pongs from successful strict samples |
| `telegram_median_ms`, `telegram_p95_ms` | Median and nearest-rank p95 of encrypted-ping round trips |
| `telegram_jitter_ms` | Mean absolute difference between consecutive measured ping RTTs |
| `reliability_lower_bound` | 95% Wilson lower bound, using reconnect attempts rather than correlated pings |
| `avg_latency_ms` | Mean successful full-probe duration, including setup; a diagnostic metric |
| `stability` | Legacy successful-probe latency consistency; one point cannot establish consistency or uptime |

The primary strict UI latency is **Telegram RTT p95**. It is not download throughput. No Mbps/MB/s value is inferred from handshake duration or ping latency. Three successes from three reconnects have an observed rate of 100% but a Wilson lower bound of about 43.8%. Shared network conditions can correlate reconnect trials, so this statistical estimate and short sample do not establish long-term uptime.

Strict scores use reconnect confidence, p95 RTT and ping jitter, with common ordering across console, exports and dashboard. The old `transport` profile checks a nonce-matched `req_pq_multi`/`resPQ`; it does not produce usable Telegram link exports. Old snapshots are labeled transport evidence and need a new strict scan. Dashboard policy/metric validation rejects inconsistent qualification claims; a saved JSON report is not itself a cryptographic attestation.

## Resource and platform limits

Strict key exchange costs more CPU/network time than a transport probe. Start with a small candidate N; default MTProto concurrency is 16, bounded independently of HTTP/SOCKS workers and by platform resource limits. Connect, authorization, ping and cleanup waits are bounded. More reconnects or pings take longer.

Public feeds and proxies can be blocked or offline. A source yielding valid syntax does not prove endpoint health. HTTP/SOCKS verification remains a certificate-verified HTTPS-origin response, without direct fallback.

The dashboard reads counts and eligible rows from one atomic snapshot generation. Stopped scans preserve completed samples and the previous full `completed_snapshot.json`. Live samples and deterministic fixture tests are recorded separately in [TESTING.md](TESTING.md). Physical Android/Termux and Docker execution remain unverified unless a later report explicitly records them.

Additional references: [Telegram transports](https://core.telegram.org/mtproto/mtproto-transports), official TDLib [TLS initialization](https://github.com/tdlib/td/blob/master/td/mtproto/TlsInit.cpp) and [TCP transport](https://github.com/tdlib/td/blob/master/td/mtproto/TcpTransport.cpp).
