"""Realistic publisher formatting must reach the independent WEB scan pool."""

from collector.telegram_webproxy import parse_feed
from telegram_webproxy.models import TelegramWebProxy


def test_markdown_operator_links_and_json_strings_are_collected_once():
    proxy = TelegramWebProxy("relay.example/path", "a" * 32)
    text = f"Public test: `{proxy.tg_link()}`\n[Connect]({proxy.web_link()})\n"
    assert [row.key() for row in parse_feed(text, "operator")] == [proxy.key()]
    import json

    assert [row.key() for row in parse_feed(json.dumps({"nested": [text]}), "operator")] == [proxy.key()]


def test_ordinary_mtproxy_and_http_proxy_are_not_web_relays():
    text = "tg://proxy?server=relay.example&port=443&secret=" + "a" * 32
    assert parse_feed(text + "\nhttp://relay.example:443", "operator") == []
