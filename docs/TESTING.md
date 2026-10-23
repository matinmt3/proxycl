# Version 4.0.0-rc.1 validation

Date: 2026-10-23. Host: Windows, Python 3.14.6. This release is a **prerelease**.
Previous release evidence is preserved in [v3.4](archive/TESTING-v3.4.md).

The integrated source suite passed **1,021 Python tests**, with **9 skips** and one optional Starlette deprecation warning. The skips are one POSIX-only lock test on Windows and eight opt-in official WEB relay cases; those eight relay cases were executed separately and all passed. An additional rollback collision regression was then added; the updater/doctor slice passed **8/8**. The JavaScript suite passed **31/31**. These are separate run counts, not additive claims.

Ruff and Black passed for the candidate; the final packaging report records extracted-artifact checks. No simulated test represents real account, device or public proxy performance evidence.

| Area | Evidence and boundary |
| --- | --- |
| Output ownership | Real subprocess lock contention and abnormal process exit, shared CLI/controller/storage tests; POSIX branch unrun on Windows |
| Evidence/ranking | Oldest-sample expiry at export/use, partial evidence, interleaved rounds, serialization and legacy fingerprints; v2 remains opt-in |
| Calibration | Matching pools, unique trials and independent 5/30-minute holdouts validated; no three-day/two-network campaign recorded |
| Personal data | Source import/export/backups, toggles, all memberships/mirrors, network favourites; CLI saved-settings roundtrip and explicit data directory |
| Installer/updater | 32 offline Bash installer cases, real Git update/rollback fixtures with mocked runtime preparation, dirty/ignored-file preservation and interrupted recovery |
| Media | Synthetic main/media/reconnect route construction, immutable route, RAM session, forbidden history/dialog RPCs, closed CDN path, budgets/partial reads/hash/ranking/logout/redaction; no real account login performed |
| WEB | Eight actual reference-relay cases, four carriers × root/subpath, trusted CA, wrong-host/untrusted-CA rejection, 5,120,000 bytes × two connections each and forced disconnect; echo backend, not Telegram |
| Browser | Real in-app Chromium actions at 320 and 390 pixels; no horizontal page overflow; themes persisted, three groups, HTTPS copy, sources/favourites, doctor download and actual empty-pool scan callback; synthetic rows explicitly labelled |
| Expired evidence | Browser verified old rows remain visible with expired label and no usable connection action |
| Review fixes | Personal-settings location, proof TTL, ignored-file collisions, pending-update guards, source refresh race, legacy probe config defaults, cancelled partial-read accounting |

The WEB reference is pinned to `c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8`. Windows-only permission-check overlays were used when building its Go binary. Product certificate validation was not disabled. See [WEB boundaries](TELEGRAM-WEB.md).

## Repeat local checks

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

The opt-in WEB harness reads fixture paths from the environment; see `tests/test_webproxy_certified_interop.py`. Test credentials and endpoints are synthetic. Source-only ZIPs intentionally exclude runtime state, output, personal data, credentials and `.git`.

## Stable promotion remains blocked by evidence gates

- Physical Android/Termux: fresh install, upgrade, real Telegram open/copy, screen-off, offline/force-stop/resume, resource use and rollback, with exact client/device versions.
- Real optional account download: selected public document, media DC migration, interruption, data cap and confirmed logout through the chosen proxy.
- At least eighteen paired policy comparisons over three days and two networks with independent 5- and 30-minute holdouts and no regression against v1.
- Actual WEB-capable Telegram client and real Telegram relay route. The native TLS/echo harness does not satisfy this gate.

The machine-readable [release gates](RELEASE-GATES.json) keep these pending. v3.4 remains latest stable; this RC does not promise complete roadmap acceptance.
