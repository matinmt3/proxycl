# Version 4.0.0-rc.2 validation

Date: 2026-10-23. Host: Windows, Python 3.14.6. This is a **prerelease**. The exact final extracted-ZIP counts, commit and hashes are in the release asset `RELEASE-VALIDATION.json`. Earlier evidence is retained in [RC1](archive/TESTING-v4-rc1.md) and [v3.4](archive/TESTING-v3.4.md).

The RC1 baseline passed 1,022 Python tests before this audit. Independent engine, media and dashboard reviews then reproduced additional failures and added regressions before fixing them. A green baseline did not establish that untested boundaries were correct.

The final RC2 source run passed **1,104 Python tests**, with **9 skips** and the dependency warning described below. The JavaScript suite passed **35/35**. These source counts are not added to the repeated extracted-artifact checks.

## Reproduced and corrected

- Saved proof must include consistent oldest/latest timestamps, TTL and expiry. Direct HTTP/SOCKS exports, terminal recommendations, media ranking and dashboard actions now reject stale or malformed evidence. Expiry is checked again after slow persistence work before publishing recommendations.
- Collection deadline exhaustion is distinct from Ctrl+C. An incomplete collection is provisional, has zero demonstrated coverage and cannot promise a resumable pool. All interrupted scan types keep provisional labels; completed scans do not advertise Resume. The dashboard accepts the actual HTTP/single-engine `testing` stage.
- A broken candidate Python runtime or pointer no longer blocks rollback. Recovery works through the launcher and with options before `update`; calibration uses the same validated runtime. Installation-wide ownership prevents updates from racing through different output directories.
- Profiles shared by different output directories cannot lose source/favourite writes. Source toggles are scoped by proxy family. Favourite actions identify the displayed candidate, and stale rank changes are rejected instead of bookmarking another endpoint.
- Media-DC imports suppress updates and close failed uncached senders. Cleanup cancellation preserves completed results. Uncertain logout is reported explicitly, expired media measurements leave current rankings, and credentials never use an echoing fallback.

## Executed checks and limits

The final release report records the full Python suites, the JavaScript suite, Ruff, Black, shell/JavaScript syntax, Python compilation, selftest, actual Windows Git/venv/dependency update and rollback, and CRC/SHA-256/manifest checks on the extracted ZIP. The ordinary Python suite skips one POSIX fork case on Windows and eight opt-in relay cases. The eight relay cases were run separately and passed. The optional FastAPI/Starlette adapter emits one dependency deprecation warning.

| Area | Evidence and boundary |
| --- | --- |
| Process/storage | Real subprocess lock contention and crash release; interrupted checkpoint/export preservation; dual installation/profile ownership and malformed-state regressions |
| Installer/update | Offline Bash installer regressions; real Git fixtures and actual isolated Windows runtime upgrade/rollback; physical Termux installation remains pending |
| Strict protocol | Pinned Telegram identity, encrypted pongs, reconnect/DC policies, FakeTLS/WEB, bounded parsing and failure classification; controlled tests plus the dated live host sample below |
| WEB | Eight pinned official relay cases: four carriers at root/subpath, trusted CA, wrong-host/untrusted-CA rejection, 5,120,000 bytes on each of two connections, forced disconnect; echo backend, not Telegram client proof |
| Media | Pinned main/media/reconnect routes, failed authorization cleanup, no history/dialog RPCs, RAM session, bounded transfer, cancellation, hashing, logout and redaction; no real account login |
| Browser | Actual Chromium actions at 320/390 pixels; scroll widths 305/375, zero console errors; source add/disable, correct favourite, theme persistence, copied HTTPS link, custom-count scan, expired actions disabled and completed Resume hint removed; synthetic data |
| Ranking | Interleaved rounds, deterministic tie/order behavior, freshness and calibration ledger validation; v1 remains default and no multi-day comparison is claimed |

## Actual Windows-host observations

A bounded live scan on 2026-10-23 fetched 51 nonempty MTProto remote feeds (plus the local manual source) and 64 nonempty HTTP/SOCKS feeds. It collected 2,512 distinct MTProto candidates and 193,985 HTTP/SOCKS candidates, but selected only ten of each for probing. Feed retrieval is not endpoint approval.

One MTProto candidate passed authenticated Telegram checks; the first 60-second run stopped during finalist testing and retained a provisional result. A real Resume on the same host/network retained five completed samples, completed all five finalist connections and produced one fully qualified result. Nine selected candidates failed the transport screen. The ten HTTP/SOCKS candidates produced no qualified result (timeouts). The report stores these outcomes without claiming phone performance, broad endpoint coverage, future availability or independent calibration.

## Repeat local verification

```bash
python -m pip install -r requirements.txt -r requirements-media.txt
python -m pytest -q
node --test tests/dashboard_client.test.cjs
python -m ruff check .
python -m black --check .
python main.py selftest
python main.py doctor
bash -n install.sh
```

The optional certified relay fixture is documented in `tests/test_webproxy_certified_interop.py`; its official reference is pinned to `c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8`. Windows permission-check overlays affect only the test Go binary; product TLS verification stays enabled.

## Stable promotion remains pending

Physical Android/Termux fresh installation, upgrade, copy/open in Telegram, screen-off, force-stop/offline/resume and rollback have not been demonstrated. Real account download/DC migration, actual WEB-compatible Telegram client behaviour, and eighteen paired comparisons across three days/two networks with independent 5/30-minute holdouts also remain pending. Public asset download/hash and Windows execution cannot replace those checks.

[Machine-readable release gates](RELEASE-GATES.json) keep these pending. Version 3.4 remains latest stable. RC2 is a corrected test candidate, not full acceptance of the roadmap.
