from collector.sources import parse_json_feed, parse_text_blob


def test_parse_text_blob_lines():
    text = """
    # comment
    1.2.3.4:443:ee0011223344556677889900aabbccdd
    5.6.7.8:8080:ff1122334455667788990011aabbccdd
    """
    proxies = parse_text_blob(text, source="test")
    assert len(proxies) == 2
    assert proxies[0].server == "1.2.3.4"
    assert proxies[0].port == 443


def test_parse_text_blob_tg_link():
    text = "Check this out: tg://proxy?server=9.9.9.9&port=443&secret=deadbeefdeadbeefdeadbeefdeadbeef"
    proxies = parse_text_blob(text, source="test")
    assert len(proxies) == 1
    assert proxies[0].server == "9.9.9.9"
    assert proxies[0].secret == "deadbeefdeadbeefdeadbeefdeadbeef"


def test_parse_json_feed_list():
    text = '[{"server": "1.1.1.1", "port": 443, "secret": "abcd1234abcd1234abcd1234abcd1234"}]'
    proxies = parse_json_feed(text, source="test")
    assert len(proxies) == 1
    assert proxies[0].server == "1.1.1.1"


def test_parse_json_feed_wrapped():
    text = '{"proxies": [{"host": "2.2.2.2", "port": 8080, "secret": "1234abcd1234abcd1234abcd1234abcd"}]}'
    proxies = parse_json_feed(text, source="test")
    assert len(proxies) == 1
    assert proxies[0].server == "2.2.2.2"
