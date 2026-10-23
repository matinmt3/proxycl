"""Personal candidate bookmarks. A bookmark never carries qualifying evidence."""

import hashlib
import re
import time

from config.loader import ScanConfig
from utils.process_lock import OutputRootLock
from utils.scan_state import deserialize_proxy, serialize_proxy
from utils.user_sources import atomic_document, canonical_mode, data_dir, read_document


class FavouriteStore:
    def __init__(self, config):
        self.config = config
        self.path = data_dir(config) / "favourites.json"

    def _load(self):
        document = read_document(self.path, {"version": 1, "rows": []})
        if (
            not isinstance(document, dict)
            or set(document) != {"version", "rows"}
            or document["version"] != 1
            or not isinstance(document["rows"], list)
            or len(document["rows"]) > 500
        ):
            raise ValueError("Invalid favourites document")
        for row in document["rows"]:
            if not isinstance(row, dict) or set(row) != {"id", "mode", "network", "proxy", "saved_at"}:
                raise ValueError("Invalid favourite")
            canonical_mode(row["mode"])
            ScanConfig(network=row["network"]).validate()
            proxy = deserialize_proxy(row["proxy"])
            if row["mode"] != (
                "webproxy"
                if getattr(proxy, "kind", None) == "webproxy"
                else "mtproto" if hasattr(proxy, "secret") else "web"
            ):
                raise ValueError("Favourite has another transport")
            expected = self.identity(proxy, row["mode"], row["network"])
            if (
                row["id"] != expected
                or type(row["saved_at"]) not in (float, int)
                or not 0 < row["saved_at"] <= time.time() + 60
            ):
                raise ValueError("Invalid favourite identity or timestamp")
        if len({row["id"] for row in document["rows"]}) != len(document["rows"]):
            raise ValueError("Duplicate favourite identity")
        return document

    @staticmethod
    def identity(proxy, mode, network):
        return hashlib.sha256(
            (canonical_mode(mode) + "\n" + network + "\n" + proxy.key()).encode()
        ).hexdigest()

    def add(self, proxy, mode, network):
        mode = canonical_mode(mode)
        network = ScanConfig(network=network).validate().network
        candidate = serialize_proxy(proxy)
        if candidate["kind"] != ("mtproto" if mode == "mtproto" else mode):
            raise ValueError("Favourite has another transport")
        identity = self.identity(proxy, mode, network)
        with OutputRootLock(self.config.output.folder, operation="favourites"):
            document = self._load()
            if any(row["id"] == identity for row in document["rows"]):
                return identity
            if len(document["rows"]) >= 500:
                raise ValueError("Remove an old favourite before adding another")
            document["rows"].append(
                {
                    "id": identity,
                    "mode": mode,
                    "network": network,
                    "proxy": candidate,
                    "saved_at": time.time(),
                }
            )
            atomic_document(self.path, document)
        return identity

    def remove(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ValueError("Invalid favourite identity")
        with OutputRootLock(self.config.output.folder, operation="favourites"):
            document = self._load()
            before = len(document["rows"])
            document["rows"] = [row for row in document["rows"] if row["id"] != identity]
            atomic_document(self.path, document)
            return len(document["rows"]) < before

    def list(self, mode, network):
        mode = canonical_mode(mode)
        network = ScanConfig(network=network).validate().network
        return [
            {
                "id": row["id"],
                "mode": mode,
                "network": network,
                "server": row["proxy"]["server"],
                "port": row["proxy"]["port"],
                "saved_at": row["saved_at"],
                "requires_recheck": True,
            }
            for row in self._load()["rows"]
            if row["mode"] == mode and row["network"] == network
        ]

    def proxies(self, mode, network):
        mode = canonical_mode(mode)
        network = ScanConfig(network=network).validate().network
        return [
            deserialize_proxy(row["proxy"])
            for row in self._load()["rows"]
            if row["mode"] == mode and row["network"] == network
        ]
