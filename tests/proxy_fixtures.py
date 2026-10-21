"""Synthetic, already authenticated results for orchestration-only tests."""

from utils.models import ProxyResult, TelegramQualityPolicy, TestSample


def qualified_result(proxy, latency=100, attempts=3):
    return ProxyResult(
        proxy,
        [
            TestSample(
                True,
                total_latency_ms=latency,
                verification_level="telegram_session",
                dc_id=(2, 4)[index % 2],
                ping_rtts_ms=[float(latency)] * 3,
            )
            for index in range(attempts)
        ],
        verification_profile="telegram_session",
        quality_policy=TelegramQualityPolicy(),
    )
