# Changelog

## 2.0.0 — 2026-10-06

- Fix MTProxy encryption keys/AES counters and require a nonce-valid req_pq/resPQ response. Add raw/dd/ee FakeTLS support, strict parsing, IPv6/base64 handling, bounded workers, and cancellation cleanup.
- Make the phone dashboard work with lightweight dependencies, open the Android browser, and handle custom output folders, invalid data, search, sorting, and narrow screens.
- Add a REVMAMAD ASCII banner, version/help/config flags, clear verified/failure counts, and resilient menu errors. Up to ten real results are displayed; unsuccessful candidates are excluded.
- Validate configuration, resolve paths independently of the launch directory, score failed proxies as zero, and atomically replace filtered exports, including empty runs.
- Improve installer retry/path/clone handling and verify dependencies before launch. Replace dead default feeds and remove malformed example proxies.
- Add regression coverage across menu/CLI, config, collection, transport, workers, ranking, output, dashboard, scheduler, and installer. See docs/TESTING.md for evidence and platform limits.

## 1.0.0

Initial public release with an automatic Termux installer and CLI menu.
