"""Transactional row-based checkpoints for large single-stage web scans.

The JSON checkpoint API stays unchanged for small or MTProto scans. This backend
keeps public candidates and finished sample evidence in separate SQLite rows;
repeated saves update only changed rows, plus one small metadata envelope.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager, suppress
from pathlib import Path

from utils.scan_state import (
    REFRESH_RETENTION_SECONDS,
    _json_tree,
    _network,
    _number,
    _pairs,
    _text,
    _timestamp,
    deserialize_result,
    serialize_result,
)
from webproxy.models import WebProxy

MAX_CANDIDATES = 1_000_000
MAX_ROW_BYTES = 2 * 1024 * 1024
MAX_DATABASE_BYTES = 4 * 1024 * 1024 * 1024
SCHEMA = 1


def _without_urls(value):
    if isinstance(value, str):
        if "://" in value:
            raise ValueError("Checkpoint must not contain feed URLs")
    elif isinstance(value, dict):
        for key, item in value.items():
            _without_urls(key)
            _without_urls(item)
    elif isinstance(value, list):
        for item in value:
            _without_urls(item)


def _dump(value) -> str:
    content = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    if len(content.encode("utf-8")) > MAX_ROW_BYTES:
        raise ValueError("SQLite checkpoint row exceeds the 2 MiB safety limit")
    return content


def _validated(value, *, now, max_age=None):
    _json_tree(value, now=now, max_age=max_age)
    _without_urls(value)
    return value


def _raw_decode(content):
    if not isinstance(content, str) or len(content.encode("utf-8")) > MAX_ROW_BYTES:
        raise ValueError("Invalid or oversized SQLite checkpoint row")
    try:
        value = json.loads(content, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise ValueError("Invalid SQLite checkpoint JSON") from exc
    return value


def _decode(content, *, now, max_age=None):
    return _validated(_raw_decode(content), now=now, max_age=max_age)


def _web_candidate(row):
    # A flat, fixed candidate schema avoids generic recursive validation and the
    # double normalization in the MTProto-compatible deserialize_proxy helper.
    fields = {"kind", "server", "port", "protocol", "source"}
    if not isinstance(row, dict) or set(row) != fields or row["kind"] != "web":
        raise ValueError("Invalid SQLite web candidate row")
    source = _text(row["source"], "proxy source", empty=True)
    _without_urls(source)
    return WebProxy(row["server"], row["port"], row["protocol"], source)


def _candidate_payload(proxy):
    row = {
        "kind": "web",
        "server": proxy.server,
        "port": proxy.port,
        "protocol": proxy.protocol,
        "source": proxy.source,
    }
    validated = _web_candidate(row)
    if (validated.server, validated.port, validated.protocol, validated.source) != (
        proxy.server,
        proxy.port,
        proxy.protocol,
        proxy.source,
    ):
        raise ValueError("SQLite candidates must use canonical endpoint settings")
    return _dump(row)


def _state_envelope(state):
    if not isinstance(state, dict) or not isinstance(state.get("engine"), dict):
        raise ValueError("SQLite web checkpoint requires an engine state")
    engine = state["engine"]
    stage = engine.get("stage")
    if (
        type(engine.get("schema")) is not int
        or engine["schema"] != 1
        or not isinstance(stage, str)
        or stage not in {"testing", "complete"}
    ):
        raise ValueError("Invalid single-stage web checkpoint engine")
    results = engine.get("results")
    if not isinstance(results, list) or len(results) > MAX_CANDIDATES:
        raise ValueError("Invalid SQLite checkpoint result list")
    envelope = {**state, "engine": {**engine, "results": []}}
    return envelope, results


class SQLiteCheckpointStore:
    def __init__(self, folder: Path, *, mode: str, network: str, fingerprint: str, max_age_seconds: float):
        if mode != "web":
            raise ValueError("SQLite checkpoint supports single-stage web scans only")
        self.mode, self.network = mode, _network(network)
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("Invalid scan configuration fingerprint")
        self.fingerprint = fingerprint
        self.max_age_seconds = _number(max_age_seconds, "max_age_seconds", minimum=1)
        self.path = Path(folder) / f"checkpoint-web-{self.network}.sqlite3"
        self._created_at = None
        self._candidate_signatures = []
        self._positions = {}
        self._result_records = {}

    @contextmanager
    def _database(self, *, write=False):
        if not write and not self.path.exists():
            raise ValueError("Saved SQLite scan is missing; start a fresh scan")
        if self.path.exists() and self.path.stat().st_size > MAX_DATABASE_BYTES:
            raise ValueError("SQLite checkpoint exceeds the database safety limit")
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.path, timeout=5)
        except sqlite3.OperationalError as exc:
            raise OSError("SQLite checkpoint storage is unavailable") from exc
        try:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_ROW_BYTES + 65536)
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            if write:
                page_size = connection.execute("PRAGMA page_size").fetchone()[0]
                connection.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}")
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("BEGIN")
            yield connection
            if write:
                connection.commit()
        except sqlite3.OperationalError as exc:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise OSError("SQLite checkpoint storage is unavailable") from exc
        except sqlite3.Error as exc:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise ValueError("Invalid, unavailable or corrupt SQLite checkpoint") from exc
        except BaseException:
            with suppress(sqlite3.Error):
                connection.rollback()
            raise
        finally:
            connection.close()

    def _metadata(self, database, *, now, enforce_age=True, allow_complete=False):
        found = database.execute("SELECT payload FROM metadata WHERE id=1").fetchone()
        if found is None:
            raise ValueError("SQLite checkpoint metadata is missing")
        max_age = self.max_age_seconds if enforce_age else None
        row = _decode(found[0], now=now, max_age=max_age)
        if (
            not isinstance(row, dict)
            or type(row.get("schema")) is not int
            or row["schema"] != SCHEMA
            or row.get("backend") != "sqlite"
        ):
            raise ValueError("Unsupported SQLite checkpoint schema")
        if row.get("mode") != self.mode or row.get("network") != self.network:
            raise ValueError("SQLite checkpoint mode/network mismatch")
        if row.get("fingerprint") != self.fingerprint:
            raise ValueError("SQLite checkpoint configuration changed; start a fresh scan")
        created = _timestamp(row.get("created_at"), "created_at", now=now, max_age=max_age)
        updated = _timestamp(row.get("updated_at"), "updated_at", now=now, max_age=max_age)
        if updated < created or type(row.get("complete")) is not bool:
            raise ValueError("Invalid SQLite checkpoint timestamps or completion flag")
        if row["complete"] and not allow_complete:
            raise ValueError("Saved SQLite scan is already complete; start a fresh scan")
        for field in ("candidate_count", "result_count"):
            if type(row.get(field)) is not int or not 0 <= row[field] <= MAX_CANDIDATES:
                raise ValueError("Invalid SQLite checkpoint row count")
        if row["result_count"] > row["candidate_count"]:
            raise ValueError("SQLite checkpoint has too many result rows")
        envelope, results = _state_envelope(row.get("state"))
        if results:
            raise ValueError("SQLite results must be stored as separate rows")
        row["state"] = envelope
        return row

    @staticmethod
    def _check_counts(database, metadata):
        for table, field in (("candidates", "candidate_count"), ("results", "result_count")):
            if database.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] != metadata[field]:
                raise ValueError("SQLite checkpoint row counts do not match metadata")

    @staticmethod
    def _schema(database):
        database.execute(
            "CREATE TABLE IF NOT EXISTS metadata (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)"
        )
        database.execute(
            "CREATE TABLE IF NOT EXISTS candidates (position INTEGER PRIMARY KEY, identity TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)"
        )
        database.execute(
            "CREATE TABLE IF NOT EXISTS results (candidate_position INTEGER PRIMARY KEY REFERENCES candidates(position), ordinal INTEGER NOT NULL, sample_count INTEGER NOT NULL, payload TEXT NOT NULL)"
        )
        database.execute("CREATE INDEX IF NOT EXISTS result_order ON results(ordinal)")

    @staticmethod
    def _signature(proxy):
        if not isinstance(proxy, WebProxy):
            raise ValueError("SQLite checkpoint candidates must be web proxies")
        return proxy.server, proxy.port, proxy.protocol, proxy.source

    def save(self, candidates: list, state: dict) -> None:
        if not isinstance(candidates, list) or not 0 <= len(candidates) <= MAX_CANDIDATES:
            raise ValueError("Invalid SQLite candidates (maximum 1000000)")
        envelope, results = _state_envelope(state)
        now = time.time()
        _validated(envelope, now=now)
        fresh = self._created_at is None
        created = now if fresh else self._created_at
        metadata = {
            "schema": SCHEMA,
            "backend": "sqlite",
            "mode": self.mode,
            "network": self.network,
            "fingerprint": self.fingerprint,
            "created_at": created,
            "updated_at": now,
            "complete": False,
            "candidate_count": len(candidates),
            "result_count": len(results),
            "state": envelope,
        }
        encoded_metadata = _dump(_validated(metadata, now=now))
        positions = {} if fresh else self._positions
        signatures = [] if fresh else list(self._candidate_signatures)
        result_records = {}
        old_by_digest = (
            {record[0]: position for position, record in self._result_records.items()} if not fresh else {}
        )
        with self._database(write=True) as database:
            self._schema(database)
            if fresh:
                database.execute("DELETE FROM results")
                database.execute("DELETE FROM candidates")
                database.execute("DELETE FROM metadata")

                def candidate_rows():
                    for index, proxy in enumerate(candidates):
                        signature = self._signature(proxy)
                        payload = _candidate_payload(proxy)
                        key = proxy.key()
                        if key in positions:
                            raise ValueError("SQLite checkpoint contains duplicate candidates")
                        positions[key] = index
                        signatures.append(signature)
                        yield index, key, payload

                database.executemany("INSERT INTO candidates VALUES(?,?,?)", candidate_rows())
            else:
                self._metadata(database, now=now, enforce_age=False)
                if len(candidates) != len(signatures):
                    raise ValueError("SQLite checkpoint candidate set changed; start a fresh scan")
                for index, proxy in enumerate(candidates):
                    signature = self._signature(proxy)
                    old = signatures[index]
                    if signature[:3] != old[:3]:
                        raise ValueError("SQLite checkpoint candidate identity changed; start a fresh scan")
                    if signature != old:
                        payload = _candidate_payload(proxy)
                        database.execute("UPDATE candidates SET payload=? WHERE position=?", (payload, index))
                        signatures[index] = signature
            for ordinal, row in enumerate(results):
                encoded = _dump(row)
                digest = hashlib.sha256(encoded.encode("utf-8")).digest()
                position = old_by_digest.get(digest)
                if position is None:
                    _validated(row, now=now)
                    result = deserialize_result(row)
                    if not isinstance(result.proxy, WebProxy):
                        raise ValueError("SQLite checkpoint result must be a web proxy")
                    position = positions.get(result.proxy.key())
                    if position is None:
                        raise ValueError("SQLite checkpoint result is not a selected candidate")
                    encoded = _dump(serialize_result(result))
                    sample_count = result.attempts
                else:
                    sample_count = self._result_records[position][2]
                if position in result_records:
                    raise ValueError("SQLite checkpoint contains duplicate results")
                previous = self._result_records.get(position) if not fresh else None
                if previous is not None and previous[0] == digest:
                    if previous[1] != ordinal:
                        database.execute(
                            "UPDATE results SET ordinal=? WHERE candidate_position=?", (ordinal, position)
                        )
                else:
                    database.execute(
                        "INSERT INTO results VALUES(?,?,?,?) ON CONFLICT(candidate_position) DO UPDATE SET ordinal=excluded.ordinal,sample_count=excluded.sample_count,payload=excluded.payload",
                        (position, ordinal, sample_count, encoded),
                    )
                result_records[position] = digest, ordinal, sample_count
            for position in self._result_records.keys() - result_records.keys() if not fresh else ():
                database.execute("DELETE FROM results WHERE candidate_position=?", (position,))
            database.execute("INSERT OR REPLACE INTO metadata VALUES(1,?)", (encoded_metadata,))
        self._created_at, self._positions = created, positions
        self._candidate_signatures, self._result_records = signatures, result_records

    def _load(self, *, for_refresh=False) -> tuple[list, dict]:
        now = time.time()
        candidates, positions, signatures, records, results = [], {}, [], {}, []
        with self._database() as database:
            metadata = self._metadata(database, now=now, enforce_age=not for_refresh)
            if for_refresh:
                _timestamp(
                    metadata["created_at"],
                    "candidate pool retention",
                    now=now,
                    max_age=REFRESH_RETENTION_SECONDS,
                )
            self._check_counts(database, metadata)
            for index, (position, identity, content) in enumerate(
                database.execute("SELECT position,identity,payload FROM candidates ORDER BY position")
            ):
                proxy = _web_candidate(_raw_decode(content))
                if (
                    position != index
                    or not isinstance(proxy, WebProxy)
                    or identity != proxy.key()
                    or identity in positions
                ):
                    raise ValueError("Invalid SQLite checkpoint candidate identity/order")
                positions[identity] = index
                signatures.append(self._signature(proxy))
                candidates.append(proxy)
            for expected, (position, ordinal, sample_count, content) in enumerate(
                database.execute(
                    "SELECT candidate_position,ordinal,sample_count,payload FROM results ORDER BY ordinal"
                )
            ):
                row = _decode(content, now=now, max_age=None if for_refresh else self.max_age_seconds)
                result = deserialize_result(row)
                if (
                    ordinal != expected
                    or type(position) is not int
                    or not 0 <= position < len(candidates)
                    or not isinstance(result.proxy, WebProxy)
                    or candidates[position].key() != result.proxy.key()
                    or type(sample_count) is not int
                    or sample_count != result.attempts
                    or position in records
                ):
                    raise ValueError("Invalid SQLite checkpoint result identity/order")
                row = serialize_result(result)
                results.append(row)
                records[position] = hashlib.sha256(_dump(row).encode("utf-8")).digest(), ordinal, sample_count
        state = metadata["state"]
        state["engine"]["results"] = results
        self._created_at, self._positions = metadata["created_at"], positions
        self._candidate_signatures, self._result_records = signatures, records
        return candidates, state

    def load(self) -> tuple[list, dict]:
        return self._load()

    def load_for_refresh(self) -> tuple[list, dict]:
        """Load an incomplete retained pool without treating old proof as fresh."""
        return self._load(for_refresh=True)

    def complete(self) -> None:
        with self._database(write=True) as database:
            metadata = self._metadata(database, now=time.time(), enforce_age=False)
            self._check_counts(database, metadata)
            metadata.update(complete=True, updated_at=time.time())
            database.execute("UPDATE metadata SET payload=? WHERE id=1", (_dump(metadata),))
