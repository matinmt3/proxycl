import base64
import json
from urllib.parse import urlencode

import pytest

from collector.sources import parse_json_feed, parse_text_blob
from tester.secrets import decode_secret

KEY = "00112233445566778899aabbccddeeff"
TLS_SECRET = "ee" + KEY + "www.example.com".encode().hex()


def test_parse_text_blob_lines():
    text = f"# comment\n1.2.3.4:443:{KEY}\n5.6.7.8:8080:dd{KEY} # score=0.8"
    proxies = parse_text_blob(text, "test")
    assert [(p.server, p.port) for p in proxies] == [("1.2.3.4", 443), ("5.6.7.8", 8080)]


def test_parse_text_blob_tg_link():
    proxies = parse_text_blob(f"Check: tg://proxy?server=9.9.9.9&port=443&secret={KEY}", "test")
    assert len(proxies) == 1
    assert proxies[0].secret == KEY


@pytest.mark.parametrize("server", ["[2001:db8::1]", "2001:db8::1"])
def test_text_ipv6_and_one_digit_port(server):
    proxy = parse_text_blob(f"{server}:1:{TLS_SECRET}", "test")[0]
    assert (proxy.server, proxy.port, proxy.secret) == ("2001:db8::1", 1, TLS_SECRET)


def test_base64_urlencoded_html_link_is_normalized_and_deduplicated():
    encoded = base64.b64encode(bytes.fromhex(TLS_SECRET)).decode()
    query = urlencode({"server": "Proxy.Example.COM.", "port": 443, "secret": encoded})
    text = f"https://t.me/proxy?{query.replace('&', '&amp;')}\nproxy.example.com:443:{TLS_SECRET}"
    proxies = parse_text_blob(text, "test")
    assert len(proxies) == 1
    assert (proxies[0].server, proxies[0].secret) == ("proxy.example.com", TLS_SECRET)


@pytest.mark.parametrize(
    "encoded", [KEY.upper(), base64.urlsafe_b64encode(bytes.fromhex(KEY)).decode().rstrip("=")]
)
def test_secret_key_bytes_survive_encoding(encoded):
    assert decode_secret(encoded).key == bytes.fromhex(KEY)


def test_fake_tls_domain_does_not_replace_the_16_byte_key():
    secret = decode_secret(TLS_SECRET)
    assert secret.key == bytes.fromhex(KEY)
    assert secret.domain == "www.example.com"
    assert secret.padded


@pytest.mark.parametrize(
    "server,port,secret",
    [
        ("1.1.1.1", 0, KEY),
        ("1.1.1.1", 65536, KEY),
        ("1.1.1.1", True, KEY),
        ("1.1.1.1", 443.5, KEY),
        ("999.1.1.1", 443, KEY),
        ("bad/host", 443, KEY),
        ("-bad.example", 443, KEY),
        ("1.1.1.1", 443, "abcd"),
        ("1.1.1.1", 443, "ee" + KEY),
        ("1.1.1.1", 443, "ff" + KEY),
        ("1.1.1.1", 443, "ee" + KEY + b"bad domain".hex()),
    ],
)
def test_invalid_candidates_are_skipped(server, port, secret):
    assert not parse_json_feed(json.dumps([{"server": server, "port": port, "secret": secret}]), "test")


def test_duplicate_query_parameters_are_rejected():
    assert not parse_text_blob(f"tg://proxy?server=a.com&server=b.com&port=443&secret={KEY}", "test")


def test_parse_json_feed_list():
    proxies = parse_json_feed(json.dumps([{"server": "1.1.1.1", "port": 443, "secret": KEY}]), "test")
    assert proxies[0].server == "1.1.1.1"


def test_parse_json_feed_wrapped_and_case_insensitive():
    proxies = parse_json_feed(
        json.dumps({"proxies": [{"HOST": "2.2.2.2", "PORT": 8080, "SECRET": KEY}]}), "test"
    )
    assert proxies[0].server == "2.2.2.2"


def test_json_feed_keeps_objects_and_string_links_even_when_an_object_parsed():
    data = {
        "proxies": [
            {"server": "1.1.1.1", "port": 443, "secret": KEY},
            f"tg://proxy?server=2.2.2.2&port=443&secret={KEY}",
        ],
        "extra": [f"3.3.3.3:443:{KEY}"],
    }
    assert [p.server for p in parse_json_feed(json.dumps(data), "test")] == ["1.1.1.1", "2.2.2.2", "3.3.3.3"]


def test_json_root_proxy_and_escaped_link():
    data = {
        "server": "1.1.1.1",
        "port": "443",
        "secret": KEY,
        "url": f"https://t.me/proxy?server=2.2.2.2&port=443&secret={KEY}",
    }
    text = json.dumps(data).replace("/", "\\/")
    assert len(parse_json_feed(text, "test")) == 2
