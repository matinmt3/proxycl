# Public source catalog

REVMAMAD ships **60 distinct enabled remote MTProto URLs**, one manual MTProto list, and **64 distinct enabled Web proxy feed URLs** in [config/sources.yaml](../config/sources.yaml). Distinct URLs can share publishers or proxy endpoints. They are discovery inputs; availability is checked on each scan.

## Configuration

The built-in config opts into `source_catalog: config/sources.yaml`. Relative catalog paths, file sources, output and log locations resolve from the config base: the project root for the conventional `config/config.yaml` layout, otherwise the external config's containing directory.

An explicit list replaces only its own mode's catalog list, including an empty list:

```yaml
source_catalog: config/sources.yaml
sources: []       # disable MTProto discovery
# Web discovery still uses the catalog.
```

Use `web_sources: []` to disable Web discovery. Older external configs without `source_catalog` load only their explicit sources and retain an empty Web source list. The optional manual list lives in `config/manual_proxies.txt`; credentials are not needed for public feed collection.

MTProto source types are `github_raw`, `http_txt`, `http_json`, `json_feed`, and `telegram_channel`. Web sources use the first four and include `protocol: http|https|socks4|socks5` for bare endpoints. Each entry also accepts `name`, `url`, and Boolean `enabled`.

## MTProto provenance and current source check

The six text feeds are [SoliSpirit](https://github.com/SoliSpirit/mtproto), [Argh94](https://github.com/Argh94/telegram-proxy-scraper), [Surfboardv2ray](https://github.com/Surfboardv2ray/TGProto), [tgmtproxy](https://github.com/tgmtproxy/mtproxy), [darkvibez456](https://github.com/darkvibez456/mtproto-proxy-auto), and [V2RAYCONFIGSPOOL](https://github.com/V2RAYCONFIGSPOOL/TELEGRAM_PROXY_SUB). We selected one V2RAYCONFIGSPOOL partition as a source, rather than counting ten partitions as ten independent publishers.

The 54 channel identities have primary provenance in [Argh94's MTProto scraper](https://github.com/Argh94/telegram-proxy-scraper/blob/main/Files/main.py), [MTProtoNexus's username catalog](https://github.com/itsyebekhe/MTProtoNexus/blob/main/usernames.json), or the [Darklord2025 MTProto collector catalog](https://github.com/Darklord2025/telegram-proxies-collector/blob/main/telegram%20channels.json). The latter list was last changed in January 2025; channel entries from it remain provisional until a scan obtains usable posts.

On **2026-10-08, 15:47:08–15:47:28 UTC**, the Windows source check obtained:

| Feed | Valid unique candidates in that feed |
| --- | ---: |
| SoliSpirit | 175 |
| Argh94 | 262 |
| Surfboardv2ray | 126 |
| tgmtproxy | 896 |
| darkvibez456 | 254 |
| V2RAYCONFIGSPOOL partition 1 | 6 |

These six feeds produced **1,281 candidates after global deduplication**. All 54 direct `t.me/s/` requests returned connection errors from this host; the manual list was empty. This documents host-specific source access and syntax parsing, not Telegram proxy health. Earlier public previews exposed valid direct URLs in NetAccount, alltelegramproxy, hotspotproxy, mtpproxy0098 and proxiteiegram, but previews can be cached.

ALIILAPRO and SoliSpirit had identical contents during research, so only SoliSpirit is enabled. An old MhdiTaheri feed yielded one valid record and was excluded from defaults. The former hookzof MTProto JSON path now contains a website pointer, so it is also excluded. Profile-only, empty and invite-only previews were excluded where observed.

## Web feed provenance and transport interpretation

The 64 selected feeds came from 19 publisher accounts. Research obtained nonempty file contents and checked endpoint sets; exact normalized mirrors and empty feeds were excluded. The catalog covers HTTP, HTTPS-capable HTTP lists, explicit TLS-to-proxy lists, SOCKS4 and SOCKS5.

Primary feed repositories include [proxifly](https://github.com/proxifly/free-proxy-list), [monosans](https://github.com/monosans/proxy-list), [Databay Labs](https://github.com/databay-labs/free-proxy-list), [Moleway](https://github.com/Moleway/Free-Proxy-List), [relayglass](https://github.com/relayglass/free-proxy-list), [litportnet](https://github.com/litportnet/free-proxy-list), [roosterkid](https://github.com/roosterkid/openproxylist), [ShiftyTR](https://github.com/ShiftyTR/Proxy-List), [vakhov](https://github.com/vakhov/fresh-proxy-list), [jetkai](https://github.com/jetkai/proxy-list), [ErcinDedeoglu](https://github.com/ErcinDedeoglu/proxies), [Zaeem20](https://github.com/Zaeem20/FREE_PROXIES_LIST), and [IPLocate](https://github.com/iplocate/free-proxy-list). Every concrete raw URL is in the YAML catalog.

A filename containing `https` often means an HTTP proxy that supports CONNECT to an HTTPS destination. Such bare endpoints use `protocol: http`. Explicit `https://` endpoint URIs retain TLS to the proxy. Databay's `https.txt` is configured as `https` because its primary README explicitly identifies TLS to the proxy. IPLocate's example alone provides weaker evidence, so its bare HTTPS group retains the conservative HTTP default.

Both the proxy-hop TLS (when applicable) and destination TLS certificates must pass verification. Publisher examples that disable certificate checks are not adopted. A discovered Web proxy is verified only when it completes its protocol handshake, certificate-verified TLS to the configured origin, and the expected origin response; the default origin is `https://www.gstatic.com/generate_204` with status 204.

## Source reports and limits

Collectors expose `source_reports` in enabled configuration order:

- `ok`: the source yielded one or more parser-valid candidates.
- `empty`: retrieval succeeded but yielded no valid candidates.
- `error`: retrieval or decoding failed; the report contains a safe error class and, for HTTP failures, status code.

Counts are per-source unique candidates before global deduplication. Disabled sources have no report. Source failures are isolated; a completed empty collection resets its reports. MTProto cancellation does not publish incomplete reports.

Both modes retain **4 MiB per source** and **eight concurrent source reads**. URLs or underlying exception strings containing private feed credentials are not included in error detail. More feeds do not guarantee more distinct or working endpoints; final protocol tests and source health are shown separately.
