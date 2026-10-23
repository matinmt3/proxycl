"""Regression checks for personal-library writes and stale dashboard actions."""

import copy
import hashlib
import threading
import time
from pathlib import Path

import pytest

from collector.health import source_identity
from config.loader import AppConfig
from dashboard import data
from dashboard.control import ControlError, ScanController
from telegram_webproxy.models import TelegramWebProxy
from tests.test_dashboard_strict import save, strict_row
from tests.test_v4_sources_ui import KEY
from utils.favourites import FavouriteStore
from utils.models import Proxy
from utils.process_lock import OutputRootBusy
from utils.user_sources import SourceStore
from webproxy.models import WebProxy


@pytest.fixture
def config(tmp_path):
    value = AppConfig.load()
    value.output.folder = str(tmp_path / "output")
    value._user_data_dir = tmp_path / "personal"
    value.sources = []
    value.web_sources = []
    value.webproxy_sources = []
    return value


@pytest.mark.parametrize("kind", ["source", "favourite"])
def test_shared_personal_directory_prevents_lost_updates_across_output_roots(config, monkeypatch, kind):
    other = copy.deepcopy(config)
    other.output.folder = str(Path(config.output.folder).with_name("another-output"))
    cls = SourceStore if kind == "source" else FavouriteStore
    first, second = cls(config), cls(other)
    entered, release = threading.Event(), threading.Event()
    load_name = "export_document" if kind == "source" else "_load"
    original = getattr(first, load_name)
    failures = []

    def held_read():
        document = original()
        entered.set()
        assert release.wait(3)
        return document

    def add(store, label):
        if kind == "source":
            return store.add_feed("mtproto", label, f"https://{label}.example/list")
        return store.add(Proxy(f"{label}.example", 443, KEY), "mtproto", "wifi")

    def write_first():
        try:
            add(first, "first")
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(first, load_name, held_read)
    worker = threading.Thread(target=write_first)
    worker.start()
    try:
        assert entered.wait(2)
        with pytest.raises(OutputRootBusy):
            add(second, "second")
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not failures
    add(second, "second")
    rows = second.list("mtproto") if kind == "source" else second.list("mtproto", "wifi")
    assert len(rows) == 2


def test_favourite_click_does_not_save_an_endpoint_that_replaced_its_rank(config):
    folder = Path(config.output.folder)
    folder.mkdir(parents=True)
    selected = strict_row()
    save(folder, [selected])
    candidate_id = data.snapshot(folder)["rows"][0]["candidate_id"]
    replacement = dict(selected, server="replacement.example")
    for key in ("tg_link", "web_link"):
        replacement[key] = replacement[key].replace(selected["server"], replacement["server"])
    save(folder, [replacement])
    controller = ScanController(config, lambda *args: None)
    try:
        with pytest.raises(ControlError, match="changed") as error:
            controller.library({"action": "favourite_add", "rank": 1, "candidate_id": candidate_id})
        assert error.value.status == 409
        assert FavouriteStore(config).list("mtproto", "default") == []
        current_id = data.snapshot(folder)["rows"][0]["candidate_id"]
        controller.library({"action": "favourite_add", "rank": 1, "candidate_id": current_id})
        assert FavouriteStore(config).list("mtproto", "default")[0]["server"] == "replacement.example"
    finally:
        controller.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://feed.example:70000/list",
        "https://feed.example:wrong/list",
        "https://feed.example:0/list",
        "https://feed\\other.example/list",
    ],
)
def test_invalid_feed_endpoint_is_rejected_before_being_saved(config, url):
    with pytest.raises(ValueError, match="URL"):
        SourceStore(config).add_feed("mtproto", "invalid", url)
    assert not SourceStore(config).path.exists()


@pytest.mark.parametrize(
    "change",
    [
        {"oldest_observed_at": None},
        {"latest_observed_at": time.time() + 600},
        {"proof_max_age_seconds": 999999},
        {"evidence_expires_at": time.time() + 999999},
        {"report_schema": 99},
    ],
)
def test_saved_expiry_cannot_promote_incomplete_or_inconsistent_proof(tmp_path, change):
    row = strict_row()
    row.update(change)
    save(tmp_path, [row])
    current = data.snapshot(tmp_path)
    assert current["rows"][0]["evidence_status"] == "invalid"
    assert current["rows"][0]["tg_link"] is None
    assert current["rows"][0]["web_link"] is None


def test_source_toggle_is_scoped_to_family_with_legacy_override_compatibility(config):
    store = SourceStore(config)
    for mode in ("mtproto", "webproxy"):
        store.add_feed(mode, "shared", "https://feed.example/list")
    mtproto = store.list("mtproto")[0]
    store.set_enabled(mtproto["id"], False)
    assert store.list("mtproto")[0]["enabled"] is False
    assert store.list("webproxy")[0]["enabled"] is True
    # Earlier v4 personal documents used the collector's feed identity globally.
    # Those saved choices remain readable until a family-specific choice replaces one.
    from utils.user_sources import apply_user_sources

    document = store.export_document()
    legacy_id = source_identity(apply_user_sources(config).sources[0])
    document["overrides"] = {legacy_id: False}
    store.import_document(document)
    assert all(row["enabled"] is False for row in store.list())
    store.set_enabled(store.list("mtproto")[0]["id"], True)
    assert store.list("mtproto")[0]["enabled"] is True
    assert store.list("webproxy")[0]["enabled"] is False


@pytest.mark.parametrize(
    "mode,candidate",
    [
        ("mtproto", Proxy("Relay.Example", 443, KEY)),
        ("telegram", Proxy("Relay.Example", 443, KEY)),
        ("webproxy", TelegramWebProxy("relay.example/base/path", KEY)),
        ("web", WebProxy("2001:db8::1", 1080, "socks5")),
    ],
)
def test_favourite_identity_roundtrip_matches_all_result_families(config, mode, candidate):
    folder = Path(config.output.folder)
    folder.mkdir(parents=True)
    row = {**strict_row(), **candidate.to_dict()}
    if mode == "web":
        row["uri"] = candidate.uri()
    else:
        row.update(tg_link=candidate.tg_link(), web_link=candidate.web_link())
    save(folder, [row], mode=mode, profile="web" if mode == "web" else "telegram_session")
    identity = data.snapshot(folder, mode)["rows"][0]["candidate_id"]
    assert identity == hashlib.sha256(candidate.key().encode()).hexdigest()
    controller = ScanController(config, lambda *args: None)
    try:
        controller.library({"action": "favourite_add", "mode": mode, "rank": 1, "candidate_id": identity})
        assert FavouriteStore(config).proxies(mode, "default")[0].key() == candidate.key()
    finally:
        controller.close()
