from utils.models import FailureReason, Proxy, ProxyResult, TestSample


def test_proxy_key_and_link():
    p = Proxy(server="1.2.3.4", port=443, secret="ee00112233445566778899aabbccddeeff0", source="test")
    assert p.key() == "1.2.3.4:443:ee00112233445566778899aabbccddeeff0"
    assert p.tg_link() == "tg://proxy?server=1.2.3.4&port=443&secret=ee00112233445566778899aabbccddeeff0"


def test_proxy_result_aggregation():
    p = Proxy(server="1.2.3.4", port=443, secret="abc123", source="test")
    result = ProxyResult(proxy=p)
    result.samples = [
        TestSample(success=True, total_latency_ms=100),
        TestSample(success=True, total_latency_ms=120),
        TestSample(success=False, failure_reason=FailureReason.TCP_TIMEOUT),
    ]

    assert result.attempts == 3
    assert result.successes == 2
    assert abs(result.success_rate - 2 / 3) < 1e-6
    assert abs(result.timeout_rate - 1 / 3) < 1e-6
    assert result.avg_latency_ms == 110
    assert result.median_latency_ms == 110


def test_empty_result():
    p = Proxy(server="1.2.3.4", port=443, secret="abc123")
    result = ProxyResult(proxy=p)
    assert result.success_rate == 0.0
    assert result.avg_latency_ms is None
    assert result.stability == 0.0


def test_proxy_key_preserves_case_sensitive_base64_secret():
    first = Proxy("Proxy.Example", 443, "AQ_abZ")
    second = Proxy("proxy.example", 443, "aq_abz")
    assert first.server == "proxy.example"
    assert first.key() != second.key()
    assert Proxy("[::1]", 443, "AABBCC").key() == "::1:443:aabbcc"


def test_telegram_link_encodes_ipv6_and_reserved_secret_characters():
    from urllib.parse import parse_qs, urlparse

    proxy = Proxy("[2001:db8::1]", 443, "AQ+/=")
    fields = parse_qs(urlparse(proxy.tg_link()).query)
    assert fields == {"server": ["2001:db8::1"], "port": ["443"], "secret": ["AQ+/="]}


def test_invalid_latency_does_not_produce_nan_exports():
    proxy = Proxy("1.2.3.4", 443, "abc123")
    result = ProxyResult(
        proxy,
        samples=[
            TestSample(True, total_latency_ms=float("nan")),
            TestSample(True, total_latency_ms=-10),
            TestSample(True, total_latency_ms=20),
        ],
    )
    assert result.avg_latency_ms == 20
    assert result.to_dict()["avg_latency_ms"] == 20
