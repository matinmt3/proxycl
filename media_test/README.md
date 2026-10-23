# Optional media measurement (experimental)

The light application never imports or installs Telethon automatically. Install
`requirements-media.txt` to opt in to the audited `Telethon==1.45.0` adapter.
Use the terminal `media-test` command after a complete, fresh strict Telegram
scan. The dashboard only reads the sanitized measurement report.

The command prompts locally, with hidden input, for your own Telegram API ID,
API hash, international phone number, login code and, when needed, two-step
password. It does not create an application, sign up an account, list dialogs,
subscribe to updates, join a channel or send messages. Provide one explicit
public channel document URL, `https://t.me/channel/message_number`. Private
messages, invitation links, link parameters and implicit channel history are
rejected. Credentials and account keys exist only in RAM; no `.session` or
downloaded file is saved. Python cannot guarantee zeroization of every RAM copy.

Defaults are five freshly qualified MTProto/Telegram WEB candidates, two fresh
downloads of the same initial 8 MiB per candidate, 512 KiB chunks, one candidate
at a time and a ten-minute measurement window. Authentication RPCs have separate
30-second limits; human typing is outside the measurement window. All observed
transport reads/writes, including login and logout, share a 100 MiB cap. This
counter covers the obfuscated MTProto byte stream and FakeTLS records; it does
not measure IP retransmissions, certified TLS overhead, WEB HTTP headers or
relay control traffic. A device's data-usage counter can therefore be higher.
The finite logout/disconnect attempt may extend wall time beyond measurement.

Main connections, media DC exports, login DC migration and reconnects use an
immutable selected-proxy connection class. Each candidate gets fresh client,
sender and packet-cipher objects; only the account authorization key/DC are
copied between MemorySessions. CDN redirects are deliberately unsupported and
fail closed before upstream can create a default-connection client. They are
reported as `cdn_unsupported`, not a failed proxy. The account restriction,
missing/changed file, budget, time limit, cancellation and transport failure
statuses are also separate and do not alter the strict protocol ranking.

Only matching complete two-round results enter the media ranking: fewer stalls,
higher median measured Mbps, then lower median time to first byte. Transfers
shorter than the target or two seconds are marked `short_sample`; they are not
long-transfer proof. The measured SHA-256 is the received prefix hash, not an
independent authoritative checksum of the publisher's entire file.

Logout is attempted through the last selected proxy, using the remaining byte
budget. `logout_status=confirmed` means Telegram acknowledged logout. A stopped
network or exhausted budget can leave `not_confirmed`; in that case remove the
"REVMAMAD optional media test" session from Telegram's Devices screen. Reports
contain only categorical outcomes, candidate hashes, network-profile label,
proof expiration and measurement numbers. They exclude account identifiers,
credentials, source URLs, selected message metadata and proxy secrets.

Automated tests exercise routing, migration connection construction, forbidden
RPCs, CDN rejection, transfer caps, partial results, logout and report redaction.
They do not prove a real account login, real Telegram media transfer, Android
execution or performance on the user's network. Those remain release gates.

Audited upstream references:

- [Telethon Connection and client contract](https://docs.telethon.dev/en/stable/modules/client.html)
- [Telethon MemorySession](https://docs.telethon.dev/en/stable/concepts/sessions.html)
- [Telegram file and DC migration protocol](https://core.telegram.org/api/files)
- Telethon 1.45.0 installed source: `network/connection/connection.py`,
  `client/telegrambaseclient.py`, `client/auth.py` and `client/downloads.py`.
