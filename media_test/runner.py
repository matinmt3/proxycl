"""Terminal-only optional media verifier with explicit account/data boundaries."""

from __future__ import annotations

import asyncio
import getpass
import hashlib
import math
import re
import statistics
import sys
import time
import warnings
from collections.abc import Callable, Sequence

from media_test.client import AuthSnapshot, Credentials, disconnect_client, get_document, make_client
from media_test.models import (
    CHUNK_BYTES,
    MEDIA_POLICY,
    REPORT_SCHEMA,
    BudgetExceeded,
    CdnUnsupported,
    EvidenceExpired,
    FileUnavailable,
    MediaSettings,
    parse_public_message_link,
)
from media_test.report import safe_report
from media_test.transport import TransferBudget, require_telethon
from telegram_webproxy.models import TelegramWebProxy
from utils.models import Proxy, ProxyResult


def select_candidates(results: Sequence[ProxyResult], settings: MediaSettings | None = None):
    settings = settings or MediaSettings()
    selected, seen = [], set()
    for result in results:
        if not isinstance(result, ProxyResult) or not isinstance(result.proxy, (Proxy, TelegramWebProxy)):
            continue
        if (
            result.verification_profile != "telegram_session"
            or result.provisional
            or not result.strict_eligible
            or not result.evidence_fresh(max_age_seconds=settings.proof_max_age_seconds)
            or result.proxy.key() in seen
        ):
            continue
        seen.add(result.proxy.key())
        selected.append(result)
        if len(selected) == settings.max_candidates:
            break
    if not selected:
        raise ValueError("Run a fresh complete strict Telegram scan before the optional media test")
    return selected


def _fresh(result, settings):
    if (
        result.provisional
        or not result.strict_eligible
        or not result.evidence_fresh(max_age_seconds=settings.proof_max_age_seconds)
    ):
        raise EvidenceExpired("expired_evidence")


def _hidden_input(prompt):
    """Refuse getpass's echoing fallback when a terminal cannot disable echo."""
    if not sys.stdin.isatty():
        raise RuntimeError("Media account login is available only in an interactive terminal")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise RuntimeError("Secure hidden terminal input is unavailable") from None


def _credentials():
    try:
        api_id = int(_hidden_input("Telegram API ID (hidden): "))
    except ValueError:
        raise ValueError("Invalid API ID") from None
    api_hash = _hidden_input("Telegram API hash (hidden): ").strip()
    phone = _hidden_input("Phone with country code (hidden): ").strip()
    if not 0 < api_id < 2**31 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ValueError("Invalid Telegram application credentials")
    if not re.fullmatch(r"\+[1-9][0-9]{5,14}", phone):
        raise ValueError("Invalid international phone number")
    return Credentials(api_id, api_hash, phone)


def failure_status(exc: BaseException, budget: TransferBudget, *, authenticating=False) -> str:
    """Only categorical outcomes leave this layer; never stringify RPC exceptions."""
    if budget.exhausted or isinstance(exc, BudgetExceeded):
        return "budget_limit"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled"
    if isinstance(exc, CdnUnsupported) or type(exc).__name__ == "_CdnRedirect":
        return "cdn_unsupported"
    if isinstance(exc, EvidenceExpired):
        return "expired_evidence"
    if isinstance(exc, FileUnavailable) or type(exc).__name__ in {
        "MessageIdInvalidError",
        "ChannelPrivateError",
        "UsernameInvalidError",
        "UsernameNotOccupiedError",
        "FileReferenceExpiredError",
        "FilerefUpgradeNeededError",
        "MediaEmptyError",
    }:
        return "file_unavailable"
    if type(exc).__name__ in {
        "FloodWaitError",
        "FloodPremiumWaitError",
        "PhoneNumberBannedError",
        "UserDeactivatedBanError",
        "AuthKeyUnregisteredError",
        "AuthKeyDuplicatedError",
        "SessionRevokedError",
        "SessionExpiredError",
    }:
        return "account_limited"
    if authenticating:
        return "authentication_failed"
    if isinstance(exc, (OSError, TimeoutError, asyncio.IncompleteReadError)):
        return "network_failed"
    return "protocol_failed"


def _progress(callback, **event):
    if callback is not None:
        callback(event)


def _remaining(deadline):
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise TimeoutError("media_time_limit")
    return min(30, remaining)


async def measure_round(client, document, settings, budget, deadline) -> dict:
    """Download the same prefix afresh. No file data is written or cached."""
    start = last = time.perf_counter()
    received, stalls, ttfb, digest = 0, 0, None, hashlib.sha256()
    target = min(settings.round_bytes, document.size)
    stream = client.iter_download(
        document,
        offset=0,
        request_size=CHUNK_BYTES,
        chunk_size=CHUNK_BYTES,
        limit=math.ceil(target / CHUNK_BYTES),
    )
    status = "partial"
    try:
        while received < target:
            if budget.exhausted:
                raise BudgetExceeded("media_budget_exhausted")
            wait = _remaining(deadline)
            chunk = await asyncio.wait_for(stream.__anext__(), wait)
            now = time.perf_counter()
            if not isinstance(chunk, (bytes, bytearray, memoryview)) or not 0 < len(chunk) <= CHUNK_BYTES:
                raise ValueError("Invalid media chunk")
            if ttfb is None:
                ttfb = (now - start) * 1000
            elif now - last >= settings.stall_seconds:
                stalls += 1
            last = now
            chunk = chunk[: target - received]
            digest.update(chunk)
            received += len(chunk)
        status = "complete"
    except StopAsyncIteration:
        status = "file_unavailable"
    except (Exception, asyncio.CancelledError) as exc:
        status = failure_status(exc, budget)
        if isinstance(exc, TimeoutError) and time.perf_counter() >= deadline:
            status = "time_limit"
    finally:
        elapsed = max(time.perf_counter() - start, 1e-9)
        try:
            await asyncio.wait_for(stream.close(), 2)
        except asyncio.CancelledError:
            status = "cancelled"
        except Exception:
            pass
    return {
        "status": status,
        "bytes": received,
        "elapsed_seconds": round(elapsed, 6),
        "ttfb_ms": round(ttfb, 3) if ttfb is not None else None,
        "stalls": stalls,
        "mbps": round(received * 8 / elapsed / 1_000_000, 6) if received else None,
        "sha256": digest.hexdigest() if received else "",
        "short_sample": received < settings.round_bytes or elapsed < 2,
    }


def _rank(rows):
    now = time.time()
    for row in rows:
        if row["status"] == "complete" and row["proof_expires_at"] <= now:
            row["status"] = "expired_evidence"
    eligible = [row for row in rows if row["status"] == "complete" and row["completed_rounds"] == 2]
    return [
        row["candidate_id"]
        for row in sorted(
            eligible,
            key=lambda row: (row["stalls"], -row["median_mbps"], row["median_ttfb_ms"], row["candidate_id"]),
        )
    ]


async def run_media_test_async(
    results,
    *,
    network_profile: str,
    message_link: str,
    settings: MediaSettings | None = None,
    progress: Callable | None = None,
    _account: Credentials | None = None,
    _client_factory=None,
    _prompt=None,
) -> dict:
    """Private injection seams support tests without an account or live traffic."""
    require_telethon()
    settings = settings or MediaSettings()
    selected = select_candidates(results, settings)
    channel, message_id = parse_public_message_link(message_link)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", network_profile):
        raise ValueError("Invalid network profile")
    account = _account or _credentials()
    prompt = _prompt or _hidden_input
    factory = _client_factory or make_client
    budget = TransferBudget(settings.budget_bytes)
    report = {
        "schema_version": REPORT_SCHEMA,
        "policy": MEDIA_POLICY,
        "experimental": True,
        "created_at": time.time(),
        "network_profile": network_profile,
        "status": "partial",
        "budget_bytes": settings.budget_bytes,
        "observed_bytes": 0,
        "downloaded_bytes": 0,
        "elapsed_seconds": 0,
        "logout_status": "not_logged_in",
        "results": [],
        "ranked_candidate_ids": [],
    }
    client, snapshot, authenticated = None, None, False
    measurement_started = None
    last_route = selected[0].proxy
    try:
        # Login traffic counts toward bytes. Human typing is excluded from
        # the 600-second measurement window; every auth RPC is bounded.
        from telethon import errors

        _fresh(selected[0], settings)
        client = factory(last_route, budget, account)
        _progress(progress, phase="authentication")
        await asyncio.wait_for(client.connect(), 30)
        await asyncio.wait_for(client.send_code_request(account.phone), 30)
        code = prompt("Telegram login code (hidden): ")
        try:
            await asyncio.wait_for(client.sign_in(account.phone, code), 30)
        except errors.SessionPasswordNeededError:
            password = prompt("Telegram two-step password (hidden): ")
            try:
                await asyncio.wait_for(client.sign_in(password=password), 30)
            finally:
                password = None
        finally:
            code = None
        authenticated = True
        report["logout_status"] = "not_confirmed"
        snapshot = AuthSnapshot.capture(client)
        await disconnect_client(client)
        client = None
        measurement_started = time.perf_counter()
        deadline = measurement_started + settings.timeout_seconds
        document_identity, reference_hash = None, None
        terminal_status = None
        for index, result in enumerate(selected):
            row = {
                "candidate_id": hashlib.sha256(result.proxy.key().encode()).hexdigest(),
                "network_profile": network_profile,
                "proof_expires_at": min(
                    result.evidence_expires_at,
                    min(sample.timestamp for sample in result.samples) + settings.proof_max_age_seconds,
                ),
                "ranking_policy": result.ranking_policy,
                "status": "partial",
                "rounds": [],
                "completed_rounds": 0,
                "median_mbps": None,
                "median_ttfb_ms": None,
                "stalls": 0,
            }
            report["results"].append(row)
            try:
                _fresh(result, settings)
                wait = _remaining(deadline)
                last_route = result.proxy
                client = factory(last_route, budget, account, snapshot)
                await asyncio.wait_for(client.connect(), wait)
                wait = _remaining(deadline)
                document = await asyncio.wait_for(get_document(client, channel, message_id), wait)
                identity = (document.id, document.size)
                if document_identity is not None and identity != document_identity:
                    row["status"] = "file_changed"
                    terminal_status = "file_changed"
                    break
                document_identity = identity
                for round_number in range(settings.rounds):
                    _fresh(result, settings)
                    _progress(progress, phase="download", candidate=index + 1, round=round_number + 1)
                    sample = await measure_round(client, document, settings, budget, deadline)
                    row["rounds"].append(sample)
                    report["downloaded_bytes"] += sample["bytes"]
                    if sample["status"] != "complete":
                        row["status"] = sample["status"]
                        break
                    if reference_hash is None:
                        reference_hash = sample["sha256"]
                    if sample["sha256"] != reference_hash:
                        row["status"] = "file_changed"
                        break
                    _fresh(result, settings)
                    row["completed_rounds"] += 1
                if row["completed_rounds"] == 2:
                    row["status"] = "complete"
                    row["median_mbps"] = statistics.median(item["mbps"] for item in row["rounds"])
                    row["median_ttfb_ms"] = statistics.median(item["ttfb_ms"] for item in row["rounds"])
                row["stalls"] = sum(item["stalls"] for item in row["rounds"])
            except (Exception, asyncio.CancelledError) as exc:
                row["status"] = failure_status(exc, budget)
                if isinstance(exc, TimeoutError) and time.perf_counter() >= deadline:
                    row["status"] = "time_limit"
            finally:
                if client is not None:
                    snapshot = AuthSnapshot.capture(client) or snapshot
                    await disconnect_client(client)
                    client = None
            _progress(progress, phase="candidate_complete", candidate=index + 1, status=row["status"])
            if row["status"] in {
                "cancelled",
                "time_limit",
                "budget_limit",
                "account_limited",
                "file_changed",
                "file_unavailable",
            }:
                terminal_status = row["status"]
                break
        report["ranked_candidate_ids"] = _rank(report["results"])
        report["status"] = terminal_status or (
            "complete" if all(row["status"] == "complete" for row in report["results"]) else "partial"
        )
    except (Exception, asyncio.CancelledError) as exc:
        report["status"] = failure_status(exc, budget, authenticating=not authenticated)
    finally:
        if measurement_started is not None:
            report["elapsed_seconds"] = round(time.perf_counter() - measurement_started, 3)
        # A cancelled sign-in may have reached Telegram before cancellation.
        # Attempt logout whenever a key exists, even if no reply confirmed it.
        if authenticated or (client is not None and AuthSnapshot.capture(client) is not None):
            report["logout_status"] = "not_confirmed"
            try:
                # Reconnect only through the last pinned route. No special
                # direct cleanup connection is permitted, even at the cap.
                if client is None:
                    client = factory(last_route, budget, account, snapshot)
                    await asyncio.wait_for(client.connect(), 5)
                if await asyncio.wait_for(client.log_out(), 5):
                    report["logout_status"] = "confirmed"
            except asyncio.CancelledError:
                report["logout_status"] = "not_confirmed"
                report["status"] = "cancelled"
            except Exception:
                report["logout_status"] = "not_confirmed"
        try:
            await disconnect_client(client)
        except asyncio.CancelledError:
            # Cleanup is outside the measurement try/except. A final Ctrl+C
            # must still return the redacted report and completed rankings.
            report["status"] = "cancelled"
        snapshot, account = None, None
        report["observed_bytes"] = budget.consumed
        # Cancellation during connection teardown can skip the ordinary loop
        # exit; completed measurements must still remain available for ranking.
        report["ranked_candidate_ids"] = _rank(report["results"])
    return safe_report(report)


def run_media_test(results, *, network_profile, message_link, settings=None, progress=None) -> dict:
    """Public synchronous terminal entry point; never called by the dashboard."""
    require_telethon()
    parse_public_message_link(message_link)
    select_candidates(results, settings)
    account = _credentials()
    return asyncio.run(
        run_media_test_async(
            results,
            network_profile=network_profile,
            message_link=message_link,
            settings=settings,
            progress=progress,
            _account=account,
        )
    )
