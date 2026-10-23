"""Non-additive source membership and observed mirror identity.

A publishing account is not proof of an independent operator. Identical candidate
sets identify observed mirrors, not common ownership. Private feed URLs never
leave this module in reports.
"""

import hashlib
import json
from urllib.parse import urlsplit

from collector.health import _safe_name, source_identity


def publisher_identity(source):
    try:
        parts = urlsplit(source.url)
    except ValueError:
        return None
    segments = parts.path.strip("/").split("/")
    account = None
    if parts.hostname in {"github.com", "raw.githubusercontent.com"} and segments[0]:
        account = "github:" + segments[0].lower()
    elif parts.hostname in {"t.me", "telegram.me"}:
        if segments[0] == "s":
            segments = segments[1:]
        if segments and segments[0]:
            account = "telegram:" + segments[0].lower()
    return hashlib.sha256(account.encode()).hexdigest() if account else None


def merge_groups(sources, groups, reports):
    """Preserve every feed membership while returning one candidate per key."""
    merged, membership = {}, {}
    for source, proxies, report in zip(sources, groups, reports):
        identity = source_identity(source)
        keys = sorted({proxy.key() for proxy in proxies})
        content_id = hashlib.sha256(json.dumps(keys).encode()).hexdigest() if keys else None
        report.update(
            name=_safe_name(source),
            source_id=identity,
            publisher_id=publisher_identity(source),
            content_id=content_id,
            fetched=report.get("fetched", report.get("status") in {"ok", "empty"}),
            parsed_unique=len(keys),
            quality_attribution="all_memberships_non_additive",
        )
        for proxy in proxies:
            proxy.source = _safe_name(source)
            saved = merged.setdefault(proxy.key(), proxy)
            ids = membership.setdefault(proxy.key(), [])
            if identity not in ids and len(ids) < 1024:
                ids.append(identity)
            saved.source_memberships = list(ids)
    for report in reports:
        content = report.get("content_id")
        report["mirror_count"] = sum(row.get("content_id") == content for row in reports) if content else 0
    return merged, membership


def source_summary(reports):
    """Counts intentionally do not claim independent publishers or add quality."""
    return {
        "configured": len(reports),
        "fetched": sum(row.get("fetched", False) for row in reports),
        "parsed_nonempty": sum(row.get("parsed_unique", 0) > 0 for row in reports),
        "observed_content_sets": len({row["content_id"] for row in reports if row.get("content_id")}),
        "known_publishing_accounts": len({row["publisher_id"] for row in reports if row.get("publisher_id")}),
        "unknown_publishers": sum(row.get("publisher_id") is None for row in reports),
    }
