# Changelog

## 3.0.0 — 2026-10-08

- Add independent MTProto and Web scan modes, ALL/custom candidate selection, deterministic source interleaving and globally deduplicated testing. The menu retains the REVMAMAD ASCII banner and offers both modes directly.
- Add 60 remote MTProto sources and 64 Web feeds with an optional YAML catalog, independent explicit overrides, bounded reads and ordered source-health reports. See docs/SOURCES.md for provenance and host-specific availability.
- Verify HTTP CONNECT, HTTPS-to-proxy, SOCKS4a and SOCKS5 through a certificate-verified HTTPS origin response. Keep certificate checks enabled and exclude direct fallback and credentialed public entries.
- Use common composite-score ordering for console and saved top ten. Isolate exports under output/mtproto and output/web; publish atomic completed-scan snapshots with honest selected/tested/verified/eligible counts.
- Replace the dashboard with local assets, separate mode tabs, mobile result cards, source/failure details, search/sort/protocol filters, copy controls, refresh/pause and clear empty/stale/corrupt states. The stdlib server remains available in lightweight installs.
- Extend regression coverage for source catalogs, four Web transports/TLS failures, workers/cancellation, mode isolation, candidate counts, ranking consistency and dashboard APIs/client behavior. Record small dated live samples separately from deterministic tests in docs/TESTING.md.

## 2.0.0 — 2026-10-06

- Fix MTProxy encryption keys/AES counters and require a nonce-valid req_pq/resPQ response. Add raw/dd/ee FakeTLS support, strict parsing, IPv6/base64 handling, bounded workers, and cancellation cleanup.
- Make the phone dashboard work with lightweight dependencies, open the Android browser, and handle custom output folders, invalid data, search, sorting, and narrow screens.
- Add a REVMAMAD ASCII banner, version/help/config flags, clear verified/failure counts, and resilient menu errors. Up to ten real results are displayed; unsuccessful candidates are excluded.
- Validate configuration, resolve paths independently of the launch directory, score failed proxies as zero, and atomically replace filtered exports, including empty runs.
- Improve installer retry/path/clone handling and verify dependencies before launch. Replace dead default feeds and remove malformed example proxies.
- Add regression coverage across menu/CLI, config, collection, transport, workers, ranking, output, dashboard, scheduler, and installer. See docs/TESTING.md for evidence and platform limits.

## 1.0.0

Initial public release with an automatic Termux installer and CLI menu.
