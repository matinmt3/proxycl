# Public source catalog

REVMAMAD ships **51 distinct enabled remote MTProto URLs**, one manual MTProto list, and **64 enabled HTTP/SOCKS feed URLs** in [config/sources.yaml](../config/sources.yaml). Distinct feeds can share publishers or proxy endpoints. They are discovery inputs; availability is checked on each scan.

The `mtproto` and `telegram` scan modes share the same `sources` catalog and MTProto protocol checks. Telegram mode presents Telegram share links; it does not create a separate source catalog or an account-login test. The `web` mode reads `web_sources` for HTTP, HTTPS-to-proxy, SOCKS4 and SOCKS5.

## Configuration

The built-in config opts into `source_catalog: config/sources.yaml`. Relative catalog paths, file sources, output and log locations resolve from the config base: the project root for the conventional `config/config.yaml` layout, otherwise the external config's containing directory.

An explicit list replaces only its own mode's catalog list, including an empty list:

```yaml
source_catalog: config/sources.yaml
sources: []       # disable discovery for MTProto and Telegram-link modes
# HTTP/SOCKS discovery still uses the Web catalog.
```

Use `web_sources: []` to disable Web discovery. Older external configs without `source_catalog` load only their explicit sources and retain an empty Web source list. The optional manual list lives in `config/manual_proxies.txt`; credentials are not needed for public feed collection.

MTProto source types are `github_raw`, `http_txt`, `http_json`, `json_feed`, and `telegram_channel`. Web sources use the first four and include `protocol: http|https|socks4|socks5` for bare endpoints. Each entry also accepts `name`, `url`, and Boolean `enabled`.

## MTProto provenance and current source check

Version 3.1 replaces the 54 direct Telegram previews that failed on the validation network. On 2026-10-08, both `t.me` and `telegram.me` resolved to a private IPv4 address and TCP port 443 refused the connection (`WinError 10061`). HTTP/TLS never started. GitHub raw files completed certificate-verified TLS and HTTP 200 on the same host. This identifies a resolver/network access problem, rather than an MTProto parser or certificate failure. Other networks can behave differently.

The defaults now use existing files from **16 repositories belonging to 15 publisher accounts**:

| Dataset group | Enabled feeds |
| --- | ---: |
| [Darklord2025 country datasets](https://github.com/Darklord2025/telegram-proxies-collector/tree/main/countries), plus its aggregate file | 31 |
| [kort0881 regional datasets](https://github.com/kort0881/telegram-proxy-collector) | 4 |
| [Therealwh aggregate/regional datasets](https://github.com/Therealwh/MTPproxyLIST) | 3 |
| Other publisher aggregate feeds | 13 |

The remaining feeds are [SoliSpirit](https://github.com/SoliSpirit/mtproto), [Argh94's scraper](https://github.com/Argh94/telegram-proxy-scraper), [Argh94's MTProto feed](https://github.com/Argh94/Proxy-List/blob/main/MTProto.txt), [Surfboardv2ray](https://github.com/Surfboardv2ray/TGProto), [tgmtproxy](https://github.com/tgmtproxy/mtproxy), [darkvibez456](https://github.com/darkvibez456/mtproto-proxy-auto), [V2RAYCONFIGSPOOL](https://github.com/V2RAYCONFIGSPOOL/TELEGRAM_PROXY_SUB), [3yed-61](https://github.com/3yed-61/MTP-Collector), [tgproxypink](https://github.com/tgproxypink/telegram-proxy-list), [dubblebyte](https://github.com/dubblebyte/free-mtproto-proxies), [Iliya3ProX](https://github.com/Iliya3ProX/good_proxies), [LoneKingCode](https://github.com/LoneKingCode/free-proxy-db), and [Telegram-FZ-LLC](https://github.com/Telegram-FZ-LLC/Telegram-Proxy). Publisher names are repository accounts, not a claim of official Telegram affiliation.

Country/region files are separately published datasets, not independently operated proxy networks. Only one V2RAYCONFIGSPOOL partition is included. Additional pool partitions, IPv4/IPv6 output variants, seven empty files and seven exact normalized mirrors were excluded from the researched candidates. The tgmtproxy repository publishes an aggregate, not per-channel mirror files.

On **2026-10-08, 18:46:30–18:46:34 UTC**, the actual configured Collector fetched all **51/51 remote feeds** successfully. Each yielded parser-valid candidates, and all 51 normalized candidate sets were distinct. After global deduplication, collection returned **2,303 candidates**; the manual file was empty. This is a source-access and syntax-parsing check, not a proxy health test or a promise of future availability.

Public channel previews remain supported for custom configs. They are no longer required by the default catalog. TLS verification stays enabled; the application does not change system DNS settings.

## HTTP/SOCKS feed provenance and transport interpretation

The 64 selected feeds came from 19 publisher accounts. Research obtained nonempty file contents and checked endpoint sets; exact normalized mirrors and empty feeds were excluded. The catalog covers HTTP, HTTPS-capable HTTP lists, explicit TLS-to-proxy lists, SOCKS4 and SOCKS5.

Primary feed repositories include [proxifly](https://github.com/proxifly/free-proxy-list), [monosans](https://github.com/monosans/proxy-list), [Databay Labs](https://github.com/databay-labs/free-proxy-list), [Moleway](https://github.com/Moleway/Free-Proxy-List), [relayglass](https://github.com/relayglass/free-proxy-list), [litportnet](https://github.com/litportnet/free-proxy-list), [roosterkid](https://github.com/roosterkid/openproxylist), [ShiftyTR](https://github.com/ShiftyTR/Proxy-List), [vakhov](https://github.com/vakhov/fresh-proxy-list), [jetkai](https://github.com/jetkai/proxy-list), [ErcinDedeoglu](https://github.com/ErcinDedeoglu/proxies), [Zaeem20](https://github.com/Zaeem20/FREE_PROXIES_LIST), and [IPLocate](https://github.com/iplocate/free-proxy-list). Every concrete raw URL is in the YAML catalog.

The unchanged HTTP/SOCKS catalog was fetched again on **2026-10-08, 18:48:26–18:48:41 UTC**: all **64/64 feeds** yielded valid candidates, with **163,372 unique candidates** after global deduplication. Counts were 70,821 HTTP, 2,101 HTTPS-to-proxy, 22,672 SOCKS4 and 67,778 SOCKS5. This check retrieved and parsed source files; it did not probe the endpoints.

A filename containing `https` often means an HTTP proxy that supports CONNECT to an HTTPS destination. Such bare endpoints use `protocol: http`. Explicit `https://` endpoint URIs retain TLS to the proxy. Databay's `https.txt` is configured as `https` because its primary README explicitly identifies TLS to the proxy. IPLocate's example alone provides weaker evidence, so its bare HTTPS group retains the conservative HTTP default.

Both the proxy-hop TLS (when applicable) and destination TLS certificates must pass verification. Publisher examples that disable certificate checks are not adopted. A discovered Web proxy is verified only when it completes its protocol handshake, certificate-verified TLS to the configured origin, and the expected origin response; the default origin is `https://www.gstatic.com/generate_204` with status 204.

## Source reports and limits

Collectors expose `source_reports` in enabled configuration order:

- `ok`: the source yielded one or more parser-valid candidates.
- `empty`: retrieval succeeded but yielded no valid candidates.
- `error`: retrieval or decoding failed; the report contains a safe error class and, for HTTP failures, status code.

Counts are per-source unique candidates before global deduplication. Disabled sources have no report. Source failures are isolated; a completed empty collection resets its reports. MTProto cancellation does not publish incomplete reports.

Both collectors retain **4 MiB per source** and **eight concurrent source reads**. URLs or underlying exception strings containing private feed credentials are not included in error detail. More feeds do not guarantee more distinct or working endpoints; final protocol tests and source health are shown separately.
