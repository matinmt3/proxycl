# Performance Tuning & Extension Notes

## Worker count vs. file descriptors

Each in-flight test holds one open TCP socket. On Linux, raise the open-file
limit before running large jobs:

```bash
ulimit -n 65535
```

A reasonable starting point:

| Proxy list size | workers | expected wall-clock (3 retries, 5s timeout) |
|---|---|---|
| < 200 | 100 | seconds |
| 200 - 2,000 | 300-500 | under a minute |
| 2,000 - 20,000 | 800-1500 | a few minutes |

## Timeout / retries trade-off

- Lower `timeout_seconds` speeds up runs but may misclassify slow-but-alive
  proxies as dead. 5s is a safe default for most networks.
- `retries` controls how many samples feed the stability/jitter calculation.
  3 is the minimum for a meaningful stability score; 5+ gives smoother
  numbers at the cost of runtime.

## Scaling beyond a single machine

The collector and tester are both pure-async and stateless per proxy, so the
proxy list can be sharded across multiple processes/machines and the
resulting `ProxyResult` objects merged before scoring/export.

## Extending validation depth

The current MTProto validator completes the obfuscated2 transport handshake
and confirms the remote behaves like a live MTProto endpoint. A deeper (and
significantly more involved) validation could additionally:

1. Complete a full `req_pq_multi` / `resPQ` exchange with the Telegram DC
   reachable through the proxy, confirming the proxy actually relays
   application-layer MTProto traffic end-to-end.
2. Cross-check the DC returned against the expected DC for the proxy's
   region.

This is intentionally left out of the default health checker to keep it
fast and dependency-light, but the `tester/mtproto.py` module is structured
so this can be added as an optional, deeper "level 2" check without
touching the collector, scorer, or exporter.
