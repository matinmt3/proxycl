"""Telegram rankings require repeated authenticated sessions and real pong latency."""

import json
from dataclasses import replace

import pytest

from benchmark.scorer import rank, score_all, score_result
from config.loader import AppConfig, OutputConfig, ScoringConfig
from exporter.exporter import Exporter, eligible_results
from utils.models import FailureReason, Proxy, ProxyResult, TestSample
from webproxy.models import WebProxy


def session_sample(dc, rtts, elapsed=1500):
    sample = TestSample(True, total_latency_ms=elapsed)
    sample.verification_level = "telegram_session"
    sample.dc_id = dc
    sample.ping_rtts_ms = rtts
    return sample


def strict_result(samples=None, server="strict.example"):
    item = ProxyResult(
        Proxy(server, 443, "01" * 16),
        (
            samples
            if samples is not None
            else [
                session_sample(2, [20, 30, 40]),
                session_sample(4, [25, 35, 45]),
                session_sample(2, [22, 32, 42]),
            ]
        ),
    )
    item.verification_profile = "telegram_session"
    item.score = score_result(item, ScoringConfig())
    return item


def test_transport_response_is_not_exported_as_a_usable_telegram_proxy():
    diagnostic = ProxyResult(
        Proxy("diagnostic.example", 443, "01" * 16), [TestSample(True, total_latency_ms=1)]
    )
    diagnostic.score = score_result(diagnostic, ScoringConfig())
    assert eligible_results([diagnostic], OutputConfig()) == []


@pytest.mark.parametrize(
    "samples",
    [
        [session_sample(2, [1, 1, 1])],
        [session_sample(2, [1, 1, 1]), session_sample(4, [1, 1, 1])],
        [session_sample(2, [1, 1, 1])] * 3,
        [session_sample(2, [1, 1]), session_sample(4, [1, 1, 1]), session_sample(2, [1, 1, 1])],
        [session_sample(2, [1, 1, float("nan")]), session_sample(4, [1, 1, 1]), session_sample(2, [1, 1, 1])],
        [session_sample(2, [10, 10, 1600]), session_sample(4, [10, 10, 10]), session_sample(2, [10, 10, 10])],
        [
            session_sample(2, [1, 1400, 1]),
            session_sample(4, [1400, 1, 1400]),
            session_sample(2, [1, 1400, 1]),
        ],
    ],
)
def test_incomplete_or_unstable_proof_is_never_promoted_even_with_zero_score_filter(samples):
    result = strict_result(samples)
    assert eligible_results([result], OutputConfig(min_score=0)) == []


def test_serialized_strict_metrics_use_pong_rtts_and_observed_connection_confidence():
    item = strict_result()
    row = item.to_dict()
    assert row.get("verification_profile") == "telegram_session"
    assert row.get("strict_eligible") is True
    assert row["strict_successes"] == 3 and row["dc_ids_verified"] == [2, 4]
    assert row["ping_count"] == 9
    assert row["telegram_median_ms"] == 32
    assert row["telegram_p95_ms"] == 45
    assert row["telegram_jitter_ms"] == 12.25
    assert row["reliability_lower_bound"] == pytest.approx(0.43849391955, abs=1e-11)
    assert row["strict_policy"] == {
        "min_successes": 3,
        "min_success_rate": 0.8,
        "required_dc_ids": [2, 4],
        "min_ping_count": 3,
        "max_p95_ms": 1500.0,
        "max_jitter_ms": 500.0,
    }
    assert eligible_results([item], OutputConfig()) == [item]
    assert json.loads(json.dumps(row, allow_nan=False))["strict_eligible"] is True


def test_repeated_verified_sessions_outrank_one_fast_reply():
    steady = strict_result(server="steady.example")
    fast = strict_result([session_sample(2, [1, 1, 1], elapsed=2)], server="quick.example")
    assert rank(score_all([fast, steady], ScoringConfig()))[0] is steady
    assert fast.stability == 0


def test_strict_score_counts_refusals_and_timeouts_as_the_same_missing_reliability():
    refused = strict_result()
    timed_out = strict_result()
    refused.samples.append(TestSample(False, failure_reason=FailureReason.TCP_REFUSED))
    timed_out.samples.append(TestSample(False, failure_reason=FailureReason.TCP_TIMEOUT))
    assert score_result(refused, ScoringConfig()) == score_result(timed_out, ScoringConfig())


def test_transport_only_success_does_not_inflate_a_strict_success_percentage():
    item = strict_result()
    item.samples.append(TestSample(True, total_latency_ms=1))
    assert item.successes == 3 and item.success_rate == 0.75
    assert eligible_results([item], OutputConfig()) == []


def test_strict_success_with_invalid_elapsed_measurement_cannot_qualify():
    item = strict_result()
    item.samples[0].total_latency_ms = float("nan")
    assert eligible_results([item], OutputConfig()) == []


def test_readable_export_reports_the_ranked_pong_latency_separately_from_setup(tmp_path):
    item = strict_result()
    path = Exporter(OutputConfig(folder=str(tmp_path))).export_best_n_readable([item])
    text = path.read_text()
    assert "Telegram RTT p95: 45 ms" in text
    assert "Setup: 1500 ms" in text
    assert "Sessions: 3/3" in text and "Matched pings: 9" in text


def test_csv_export_retains_strict_proof_and_quality_metrics(tmp_path):
    import csv

    item = strict_result()
    path = Exporter(OutputConfig(folder=str(tmp_path))).export_csv([item])
    with path.open(newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row.get("verification_profile") == "telegram_session"
    assert row["strict_eligible"] == "True"
    assert row["strict_successes"] == "3" and row["ping_count"] == "9"
    assert float(row["telegram_p95_ms"]) == 45


def test_web_single_success_eligibility_is_unchanged():
    item = ProxyResult(WebProxy("web.example", 8080), [TestSample(True, total_latency_ms=25)])
    item.score = score_result(item, ScoringConfig())
    assert eligible_results([item], OutputConfig()) == [item]


def test_strict_quality_policy_survives_snapshot_copy_without_changing_positional_score():
    item = strict_result()
    copied = replace(item, samples=list(item.samples))
    assert getattr(copied, "verification_profile", None) == "telegram_session"
    assert copied.to_dict() == item.to_dict()
    assert ProxyResult(item.proxy, [], 0.7).score == 0.7


def test_older_configuration_loads_new_strict_defaults(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text("sources: []\nweb_sources: []\n")
    config = AppConfig.load(path)
    assert getattr(config.testing, "mtproto_profile", None) == "telegram_session"
    assert config.testing.mtproto_workers == 16
    assert config.testing.telegram_dc_ids == [2, 4]
    assert config.testing.auth_timeout_seconds == 12
    assert config.testing.ping_timeout_seconds == 3
    assert config.testing.ping_count == 3 and config.testing.ping_interval_seconds == 0.5
    assert config.output.mtproto_min_successes == 3


@pytest.mark.parametrize(
    "settings,field",
    [
        ("testing: {mtproto_profile: invalid}", "mtproto_profile"),
        ("testing: {mtproto_profile: [transport]}", "mtproto_profile"),
        ("testing: {mtproto_workers: 0}", "mtproto_workers"),
        ("testing: {auth_timeout_seconds: false}", "auth_timeout_seconds"),
        ("testing: {auth_timeout_seconds: 121}", "auth_timeout_seconds"),
        ("testing: {ping_timeout_seconds: 0}", "ping_timeout_seconds"),
        ("testing: {ping_timeout_seconds: 61}", "ping_timeout_seconds"),
        ("testing: {ping_count: 0}", "ping_count"),
        ("testing: {ping_count: 2}", "ping_count"),
        ("testing: {ping_count: 11}", "ping_count"),
        ("testing: {ping_interval_seconds: -1}", "ping_interval_seconds"),
        ("testing: {ping_interval_seconds: 5.1}", "ping_interval_seconds"),
        ("testing: {telegram_dc_ids: []}", "telegram_dc_ids"),
        ("testing: {telegram_dc_ids: [2]}", "telegram_dc_ids"),
        ("testing: {telegram_dc_ids: [2, 2]}", "telegram_dc_ids"),
        ("testing: {telegram_dc_ids: [true, 4]}", "telegram_dc_ids"),
        ("testing: {telegram_dc_ids: [2, 6]}", "telegram_dc_ids"),
        ("output: {mtproto_min_successes: 0}", "mtproto_min_successes"),
        ("output: {mtproto_min_successes: 2}", "mtproto_min_successes"),
        ("output: {mtproto_min_success_rate: 0.7}", "mtproto_min_success_rate"),
        ("output: {mtproto_min_success_rate: 1.1}", "mtproto_min_success_rate"),
        ("output: {mtproto_max_ping_p95_ms: 0}", "mtproto_max_ping_p95_ms"),
        ("output: {mtproto_max_ping_jitter_ms: false}", "mtproto_max_ping_jitter_ms"),
    ],
)
def test_strict_config_rejects_invalid_policy_values(tmp_path, settings, field):
    path = tmp_path / "settings.yaml"
    path.write_text(settings)
    with pytest.raises(ValueError, match=field):
        AppConfig.load(path)
