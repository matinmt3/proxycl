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
