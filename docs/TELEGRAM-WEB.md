# Telegram WEB protocol research and implementation

Research date: **2026-10-23**. Official reference server reviewed at commit **c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8**:

- [Wire contract](https://github.com/telegramdesktop/tproxy-server/blob/c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8/PROTOCOL.md)
- [Base paths and share-link marker](https://github.com/telegramdesktop/tproxy-server/blob/c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8/BASE_PATH.md)
- [Android design](https://github.com/telegramdesktop/tproxy-server/blob/c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8/ANDROID.md)
- [Desktop client design](https://github.com/telegramdesktop/tdesktop/blob/dev/docs/web-proxy-plan.md) (moving development branch)

## What the pasted announcement gets right

WEB is a distinct Telegram proxy type. The existing MTProxy encryption is carried inside a web relay; the reference supports certified HTTPS polling/lanes and WSS/lanes. An official client uses an embedded platform web engine. That changes the carrier seen on the network, but does not establish undetectability or universal client rollout. Client compatibility and network filtering must be tested separately.

Ordinary `https://t.me/proxy` shares still identify MTProxy. HTTP/SOCKS internet proxies remain a separate mode. REVMAMAD calls the new type `webproxy` consistently across CLI, state, source catalog, exports and dashboard.

## Supported contract

- Canonical DNS/IDNA host, optional canonical base path, implicit HTTPS port 443; reject IP literals, numeric shorthand, credentials, ambiguous encodings and malformed query fields.
- Raw 16-byte and dd inner MTProxy secrets; ee/FakeTLS inner secrets are unsupported in this WEB contract. Base-path share links use the specified 0x70 marker in unpadded base64url.
- Derive the host/path-bound HMAC capability locally; read only the reference bridge's fixed literal configuration. Do not evaluate remote JavaScript or expose a raw secret to it.
- Bootstrap and authenticate a relay session, validate HELLO/WELCOME and use bounded binary mux frames, stream windows, exact cursors and byte-identical retry sequences. Certified TLS remains enabled; no direct MTProto fallback or environment-proxy detour is used for the carrier.
- Implement all four reference carriers: HTTPS, HTTPS lanes, WebSocket and WebSocket lanes. Cancel and drain owned tasks and close HTTP/socket resources on stop.
- Send the existing obfuscated MTProxy stream through the relay and require pinned-key RSA/DH plus matched encrypted pongs before strict qualification. HTTP 200, a site, a relay WELCOME or WSS 101 alone is insufficient.

## Measured boundaries

The native Python/OpenSSL client does not reproduce a platform WebView's TLS fingerprint, page lifecycle, cookie environment or all browser retry behavior. Transient native network errors may fail a strict attempt sooner than the official bridge's longer retry policy. REVMAMAD verifies wire interoperability and current Telegram response through the configured relay; it cannot promise identical DPI resistance, real app connection quality or download speed.

The primary action is `tg://webproxy`. A WEB-capable Telegram client is required. The public `t.me/webproxy` frontend may currently route to an unrelated channel in a browser, so HTTPS share copying is secondary and explicitly labeled. No minimum client version is asserted from a third-party guide.

## Reference interoperability evidence

For v4 RC, the additional certified-TLS harness passed **8/8 cases** (four carriers × root/subpath). A locally trusted test CA and real TLS frontend were used; the default untrusted-CA path and a wrong hostname were both rejected. Each case transferred 5,120,000 bytes across each of two fresh connections, with a forced drop between them: 81,920,000 payload bytes total. The official Go relay still uses the Windows permission-check-only overlay. These checks use an echo backend; they do not establish a public Telegram route or physical device compatibility.

| Client evidence | Version actually tested | Status |
| --- | --- | --- |
| Native REVMAMAD / Python on Windows | 4.0.0-rc.1 / 3.14.6 | Controlled TLS relay contract passed |
| Telegram Android on a physical phone | None recorded | Pending; no compatibility minimum claimed |
| Telegram Desktop WEB-capable client | None recorded | Pending |

The following paragraph preserves the earlier v3.4 cleartext harness evidence:

All four carrier profiles passed at the root and at a base path against the pinned official Go relay's actual auth/wire handlers: **8/8 cases**, each echoing **5,120,000 bytes**, beyond the 4 MiB stream window. On Windows the isolated reference build overlaid only Unix permission-bit checks (Windows reports 0666 rather than 0600); those test-only overlays are not shipped in this product. The relay ran on cleartext loopback behind a simulated TLS front and an echo backend. This is controlled wire/flow-control evidence, not an unmodified Linux deployment, public TLS/Telegram session, media download or physical Android pass.

Deterministic tests separately cover certified carrier request policy, malformed bootstrap/upgrade/frame/cursor/window behavior, cancellation, strict Telegram proof and full collector/export/checkpoint/recheck integration. See [release evidence](TESTING.md) for complete suite totals, actual public observations and remaining device limits.
