from benchmark.scorer import rank, score_all, score_result
from config.loader import ScoringConfig
from utils.models import FailureReason, Proxy, ProxyResult, TestSample


def make_result(latencies, failures=0, port=443) -> ProxyResult:
    p = Proxy(server="1.2.3.4", port=port, secret="abc123")
    r = ProxyResult(proxy=p)
    for lat in latencies:
        r.samples.append(TestSample(success=True, total_latency_ms=lat))
    for _ in range(failures):
        r.samples.append(TestSample(success=False, failure_reason=FailureReason.TCP_TIMEOUT))
    return r


def test_score_result_perfect_proxy():
    cfg = ScoringConfig()
    r = make_result([10, 12, 11])
    score = score_result(r, cfg)
    assert 0.9 <= score <= 1.0


def test_score_result_bad_proxy():
    cfg = ScoringConfig()
    r = make_result([], failures=3)
    score = score_result(r, cfg)
    assert score < 0.2


def test_rank_orders_descending():
    cfg = ScoringConfig()
    good = make_result([10, 12], port=1)
    bad = make_result([], failures=2, port=2)
    results = score_all([bad, good], cfg)
    ranked = rank(results)
    assert ranked[0].proxy.port == 1
    assert ranked[-1].proxy.port == 2
