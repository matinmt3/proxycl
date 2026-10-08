# Protocol and performance notes

Version 3 retains the `req_pq_multi` / `resPQ` verification exchange without account login. Raw obfuscated transport, `dd` padded-intermediate, and `ee` FakeTLS have distinct framing. Encryption keys, stream counters, response structure, and request nonce are checked. Success is a point-in-time result, not a guarantee that an authorized Telegram session will work later.

Connection and handshake timeouts are bounded. Retries collect independent latency samples; more retries take longer. Worker count is capped against available file descriptors when the platform exposes the limit. Phones can start around 50–100 workers and adjust for their network and workload.

Scheduler intervals use a monotonic clock. Output replacement is atomic for dashboard readers. Exports exclude candidates with no successful sample and apply score/latency limits.

Public sources and proxies may be blocked or offline. Inspect counts and failure reasons rather than assuming a requested top-N list can always be filled.

Regression tests use deterministic local transport servers. Live checks vary with the network and are recorded separately in TESTING.md. Native Android package installation and browser-intent delivery still require testing on a physical phone.

Protocol references: [Telegram transports](https://core.telegram.org/mtproto/mtproto-transports), [authorization handshake](https://core.telegram.org/mtproto/auth_key), and official TDLib [TLS initialization](https://github.com/tdlib/td/blob/master/td/mtproto/TlsInit.cpp) / [TCP transport](https://github.com/tdlib/td/blob/master/td/mtproto/TcpTransport.cpp).
