"""Personal data, provenance and authenticated dashboard flows across boundaries."""

import asyncio
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from collector.collector import Collector
from collector.health import source_identity
from collector.provenance import source_summary
from collector.telegram_webproxy import TelegramWebCollector
from config.loader import AppConfig, SourceConfig
from dashboard import data
from dashboard.control import ControlError, ScanController
from dashboard.server import create_server
from tests.test_dashboard_strict import save, strict_row
from utils.favourites import FavouriteStore
from utils.models import Proxy
from utils.process_lock import OutputRootBusy, OutputRootLock
from utils.user_sources import SourceStore, apply_user_sources
from webproxy.collector import WebCollector

KEY = "00112233445566778899aabbccddeeff"


@pytest.fixture
def config(tmp_path):
    value = AppConfig.load()
    value.output.folder = str(tmp_path / "output")
    value._user_data_dir = tmp_path / "personal"
    value.sources = []
    value.web_sources = []
    value.webproxy_sources = []
    return value


@pytest.mark.parametrize(
    "mode,collector,text",
    [
        ("mtproto", Collector, f"tg://proxy?server=relay.example&port=443&secret={KEY}"),
        ("web", WebCollector, "socks5://relay.example:1080"),
        ("webproxy", TelegramWebCollector, f"tg://webproxy?server=relay.example&secret={KEY}"),
    ],
)
def test_manual_source_import_is_consumed_and_toggle_survives_reload(config, mode, collector, text):
    store = SourceStore(config)
    assert store.import_manual(mode, text, "my links")
    assert not store.import_manual(mode, text, "my links")
    original = list(config.sources)
    overlay = apply_user_sources(config)
    candidates = asyncio.run(collector(overlay).collect_async())
    assert len(candidates) == 1 and len(candidates[0].source_memberships) == 1
    assert config.sources == original
    identity = store.list(mode)[0]["id"]
    store.set_enabled(identity, False)
    assert asyncio.run(collector(apply_user_sources(overlay)).collect_async()) == []
    store.set_enabled(identity, True)
    assert len(asyncio.run(collector(apply_user_sources(config)).collect_async())) == 1


def test_export_import_backup_preserves_shipped_manual_source(config, tmp_path):
    manual = tmp_path / "v34-manual.txt"
    manual.write_text(f"old.example:443:{KEY}\n")
    config.sources = [SourceConfig("old", "http_txt", "file://" + str(manual))]
    store = SourceStore(config)
    store.add_feed("mtproto", "new", "https://example.test/proxies.txt")
    exported = store.export_document()
    store.set_enabled(store.list()[0]["id"], False)
    store.import_document(exported)
    assert store.path.with_suffix(".backup.json").is_file()
    assert store.export_document() == exported
    assert manual.read_text().startswith("old.example")
    assert len(store.list("mtproto")) == 2


@pytest.mark.parametrize(
    "row",
    [
        {"mode": "web", "name": "bad", "url": "file:///etc/passwd"},
        {"mode": "mtproto", "name": "bad", "url": "https://user:pass@example.test/"},
        {"mode": "webproxy", "name": "bad", "manual": "http://relay.example:8080"},
        {"mode": "mtproto", "name": "bad", "manual": "../../private.txt"},
        {"mode": "mtproto", "name": "bad", "url": "https://example.test/", "protocol": "socks5"},
    ],
)
def test_import_rejects_paths_credentials_cross_family_and_bad_schema(config, row):
    with pytest.raises(ValueError):
        SourceStore(config).import_document({"version": 1, "sources": [row], "overrides": {}})
    assert not SourceStore(config).path.exists()


def test_personal_mutation_is_blocked_by_another_scan_thread(config):
    entered, release = threading.Event(), threading.Event()

    def scan():
        with OutputRootLock(config.output.folder):
            entered.set()
            release.wait(2)

    worker = threading.Thread(target=scan)
    worker.start()
    try:
        assert entered.wait(1)
        with pytest.raises(OutputRootBusy):
            SourceStore(config).add_feed("mtproto", "feed", "https://example.test/")
        with pytest.raises(OutputRootBusy):
            FavouriteStore(config).add(Proxy("relay.example", 443, KEY), "mtproto", "wifi")
    finally:
        release.set()
        worker.join(2)


@pytest.mark.asyncio
async def test_all_memberships_and_identical_content_do_not_multiply_candidates(config, monkeypatch):
    config.sources = [
        SourceConfig("one", "http_txt", "https://raw.githubusercontent.com/alice/a/main/list.txt"),
        SourceConfig("mirror", "http_txt", "https://github.com/alice/b/raw/main/list.txt"),
        SourceConfig("other", "http_txt", "https://example.test/list"),
    ]

    async def fetch(self, client, url):
        return f"relay.example:443:{KEY}\n"

    monkeypatch.setattr(Collector, "_fetch_url", fetch)
    collector = Collector(config)
    rows = await collector.collect_async()
    assert len(rows) == 1
    assert rows[0].source_memberships == [source_identity(source) for source in config.sources]
    assert all(row["mirror_count"] == 3 for row in collector.source_reports)
    summary = source_summary(collector.source_reports)
    assert summary["observed_content_sets"] == 1
    assert summary["known_publishing_accounts"] == 1
    assert summary["unknown_publishers"] == 1
    assert KEY not in json.dumps(collector.source_reports)


def test_expired_and_legacy_results_remain_visible_but_never_actionable(tmp_path):
    for expiry, expected in [(None, "missing"), (time.time() - 1, "expired"), (time.time() + 500, "fresh")]:
        row = strict_row()
        if expiry is None:
            for key in (
                "oldest_observed_at",
                "latest_observed_at",
                "proof_max_age_seconds",
                "evidence_expires_at",
            ):
                row.pop(key)
        else:
            row.update(
                oldest_observed_at=expiry - 3600, latest_observed_at=expiry - 3599, evidence_expires_at=expiry
            )
        save(tmp_path, [row])
        current = data.snapshot(tmp_path)
        assert current["state"] == "ready"
        assert current["rows"][0]["evidence_status"] == expected
        assert bool(current["rows"][0]["tg_link"]) == (expected == "fresh")


def test_favourites_alias_network_isolation_and_fresh_worker(config):
    candidate = Proxy("relay.example", 443, KEY)
    store = FavouriteStore(config)
    identity = store.add(candidate, "telegram", "wifi")
    assert store.add(candidate, "mtproto", "wifi") == identity
    assert len(store.list("telegram", "wifi")) == 1
    assert not store.list("mtproto", "mobile")
    assert "strict_eligible" not in store.path.read_text()
    received, done = [], threading.Event()

    def execute(current, mode, limit, command, resume, stop):
        received.append((current._favourite_proxies, mode, command, resume))
        done.set()

    controller = ScanController(config, execute)
    try:
        controller.library({"action": "favourite_recheck", "mode": "mtproto", "network": "wifi"})
        assert done.wait(2)
        assert received[0][0][0].key() == candidate.key()
        assert received[0][1:] == ("mtproto", "best10", False)
    finally:
        controller.close()
    assert store.remove(identity)
    assert not store.list("mtproto", "wifi")


def test_library_http_requires_exact_origin_and_token_then_roundtrips(config):
    controller = ScanController(config, lambda *args: None)
    server = create_server("127.0.0.1", 0, config.output.folder, controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(body, token=None, origin=None):
        headers = {"Content-Type": "application/json", "Origin": origin or base}
        if token:
            headers["X-Revmamad-Token"] = token
        return urlopen(Request(base + "/api/library", data=json.dumps(body).encode(), headers=headers))

    try:
        for token, origin in [(None, base), (controller.token, "https://evil.test")]:
            with pytest.raises(HTTPError) as error:
                request({"action": "list"}, token, origin)
            assert error.value.code == 403
        with request(
            {"action": "source_manual", "mode": "mtproto", "text": f"relay.example:443:{KEY}"},
            controller.token,
        ) as response:
            assert json.load(response)["added"]
        with request({"action": "list"}, controller.token) as response:
            payload = json.load(response)
            assert payload["sources"][0]["personal"]
            assert KEY not in json.dumps(payload)
        with pytest.raises(HTTPError) as error:
            request({"action": "source_add", "url": "file:///etc/passwd", "name": "bad"}, controller.token)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
        controller.close()


def test_favourite_add_uses_current_fresh_snapshot_not_client_proxy(config):
    folder = __import__("pathlib").Path(config.output.folder)
    folder.mkdir(parents=True)
    save(folder, [strict_row()])
    controller = ScanController(config, lambda *args: None)
    try:
        added = controller.library({"action": "favourite_add", "rank": 1})
        assert added["id"] == FavouriteStore(config).list("mtproto", "default")[0]["id"]
        with pytest.raises(ControlError):
            controller.library({"action": "favourite_add", "rank": 1, "proxy": {"server": "evil"}})
        row = strict_row()
        row["evidence_expires_at"] = time.time() - 1
        save(folder, [row])
        with pytest.raises(ControlError, match="Recheck"):
            controller.library({"action": "favourite_add", "rank": 1})
    finally:
        controller.close()


def test_media_report_http_is_read_only_allowlisted_and_network_scoped(config):
    from pathlib import Path

    document = {
        "schema_version": 1,
        "status": "complete",
        "network_profile": "default",
        "created_at": time.time(),
        "logout_status": "confirmed",
        "phone": "PRIVATE_PHONE",
        "session": "PRIVATE_SESSION",
        "results": [
            {
                "candidate_id": "a" * 64,
                "network_profile": "default",
                "status": "complete",
                "proof_expires_at": time.time() + 300,
                "completed_rounds": 2,
                "rounds": [],
                "password": "PRIVATE_PASSWORD",
            },
            {
                "candidate_id": "b" * 64,
                "network_profile": "mobile",
                "status": "complete",
                "proof_expires_at": time.time() + 300,
                "completed_rounds": 2,
                "rounds": [],
            },
        ],
        "ranked_candidate_ids": ["a" * 64, "b" * 64],
    }
    folder = Path(config.output.folder) / "mtproto"
    folder.mkdir(parents=True)
    path = folder / "media-report.json"
    path.write_text(json.dumps(document))
    before = path.read_bytes()
    controller = ScanController(config, lambda *args: None)
    server = create_server("127.0.0.1", 0, config.output.folder, controller)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        request = Request(
            base + "/api/library",
            data=json.dumps({"action": "media_report", "mode": "mtproto", "network": "default"}).encode(),
            headers={
                "Origin": base,
                "Content-Type": "application/json",
                "X-Revmamad-Token": controller.token,
            },
        )
        with urlopen(request) as response:
            report = json.load(response)["report"]
        assert report["ranked_candidate_ids"] == ["a" * 64]
        assert len(report["results"]) == 1
        assert "PRIVATE" not in json.dumps(report)
        assert controller.status()["job"] is None
        assert controller.library({"action": "media_report", "mode": "mtproto", "network": "mobile"}) == {
            "report": None
        }
        with pytest.raises(ControlError):
            controller.library({"action": "media_report", "mode": "mtproto", "path": str(path)})
        assert path.read_bytes() == before
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
        controller.close()
