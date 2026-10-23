"""Canonical WEB addresses and capability/share-link derivation.

Wire contract: telegramdesktop/tproxy-server PROTOCOL.md and BASE_PATH.md,
commit c8adb8b7c6b7fc46c12ae3acb68be9070c26a8e8 (researched 2026-10-23).
No upstream implementation is copied here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlencode

import idna

from tester.secrets import decode_secret


def normalize_address(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or value != value.strip() or len(value) > 512:
        raise ValueError("Invalid Telegram WEB address")
    hostname, separator, path = value.partition("/")
    if not hostname or any(char in hostname for char in "%:@?#\\") or hostname.endswith("."):
        raise ValueError("WEB proxy requires a DNS hostname")
    try:
        # Match browser host processing, including nontransitional sharp-s
        # handling, before deciding whether the address is a DNS hostname.
        hostname = (
            idna.encode(hostname, uts46=True, transitional=False, std3_rules=True).decode("ascii").lower()
        )
    except UnicodeError as exc:
        raise ValueError("Invalid WEB hostname") from exc
    if len(hostname) > 253 or "." not in hostname or hostname.endswith("."):
        raise ValueError("Invalid WEB hostname")
    try:
        ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        raise ValueError("WEB proxy requires a DNS hostname")
    # A WHATWG URL host ending in an IPv4 number is parsed as an IPv4
    # address, including shortened, octal and hexadecimal representations.
    # Reject it instead of testing one host and exporting a browser link to
    # another host. The bare hexadecimal prefix denotes zero in that parser.
    if re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]*)", hostname.rsplit(".", 1)[-1]):
        raise ValueError("WEB proxy requires a DNS hostname")
    for label in hostname.split("."):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            raise ValueError("Invalid WEB hostname")
    if separator and (
        not path
        or len(path) > 128
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:/[A-Za-z0-9][A-Za-z0-9_-]*)*", path)
    ):
        raise ValueError("Invalid WEB base path")
    return hostname, path


def decode_web_secret(value: str, *, path: bool = False) -> str:
    if not isinstance(value, str) or not value or len(value) > 600:
        raise ValueError("Invalid WEB secret")
    marked = False
    is_hex = bool(re.fullmatch(r"[0-9A-Fa-f]+", value)) and len(value) in (32, 34)
    if not is_hex and re.fullmatch(r"[A-Za-z0-9_-]+", value):
        try:
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        except ValueError:
            raw = b""
        if len(raw) >= 17 and raw[0] == 0x70:
            # Require a canonical marked base64url form, not another spelling.
            if base64.urlsafe_b64encode(raw).rstrip(b"=").decode() != value:
                raise ValueError("Noncanonical marked WEB secret")
            marked = True
            value = raw[1:].hex()
    if path and not marked:
        raise ValueError("A path-bearing WEB link requires the 0x70 secret marker")
    secret = decode_secret(value)
    if secret.domain:
        raise ValueError("WEB supports raw or dd MTProxy secrets, not FakeTLS")
    return secret.encoded


@dataclass
class TelegramWebProxy:
    """One certified HTTPS relay plus its ordinary inner MTProxy secret."""

    server: str
    secret: str
    source: str = "unknown"
    tag: str | None = None
    port: int = 443
    protocol: str = "webproxy"
    kind: str = "webproxy"

    def __post_init__(self):
        self.hostname, self.base_path = normalize_address(self.server)
        if self.port != 443 or type(self.port) is not int:
            raise ValueError("Telegram WEB uses certified HTTPS on port 443")
        if self.protocol != "webproxy" or self.kind != "webproxy":
            raise ValueError("Invalid WEB protocol")
        self.server = self.hostname + ("/" + self.base_path if self.base_path else "")
        self.secret = decode_web_secret(self.secret)

    @property
    def host(self) -> str:
        return self.hostname

    @property
    def origin(self) -> str:
        return "https://" + self.hostname

    @property
    def relay_base(self) -> str:
        return self.origin + "/" + (self.base_path + "/" if self.base_path else "")

    @property
    def bridge_capability(self) -> str:
        context = (
            "tdesktop-web-proxy-bridge-v2\n" + self.hostname + "\n" + self.base_path
            if self.base_path
            else "tdesktop-web-proxy-bridge-v1\n" + self.hostname
        )
        digest = hmac.new(bytes.fromhex(self.secret), context.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    @property
    def bridge_url(self) -> str:
        return self.relay_base + "?bridge=" + self.bridge_capability

    def key(self) -> str:
        return f"webproxy:{self.server}:443:{self.secret}"

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "protocol": self.protocol,
            "server": self.server,
            "port": self.port,
            "secret": self.secret,
            "source": self.source,
            "tag": self.tag,
        }

    def _query(self) -> str:
        secret = self.secret
        if self.base_path:
            secret = base64.urlsafe_b64encode(b"\x70" + bytes.fromhex(secret)).rstrip(b"=").decode()
        return urlencode({"server": self.server, "secret": secret})

    def tg_link(self) -> str:
        return "tg://webproxy?" + self._query()

    def web_link(self) -> str:
        return "https://t.me/webproxy?" + self._query()
