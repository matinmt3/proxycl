"""Decode Telegram proxy secrets without truncating keys or TLS domains.

Wire formats follow TDLib's td/mtproto/ProxySecret.cpp: 16 raw bytes,
0xdd + 16 bytes, or 0xee + 16 bytes + a domain. Encodings are hex/base64url.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProxySecret:
    key: bytes
    padded: bool = False
    domain: str | None = None
    encoded: str = ""


def normalize_server(server: str) -> str:
    if not isinstance(server, str):
        raise ValueError("Proxy server must be a hostname or IP address")
    value = server.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if not value or "%" in value or value.endswith(".."):
        raise ValueError("Invalid proxy server")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        pass
    if re.fullmatch(r"[0-9.]+", value):
        raise ValueError("Invalid proxy IP address")
    try:
        value = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Invalid proxy hostname") from exc
    if len(value) > 253 or not value:
        raise ValueError("Invalid proxy hostname")
    for label in value.split("."):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label):
            raise ValueError("Invalid proxy hostname")
    return value


def normalize_port(port: object) -> int:
    if isinstance(port, bool) or not isinstance(port, (str, int)):
        raise ValueError("Proxy port must be an integer")
    if isinstance(port, str) and not re.fullmatch(r"[0-9]{1,5}", port.strip()):
        raise ValueError("Invalid proxy port")
    number = int(port)
    if not 1 <= number <= 65535:
        raise ValueError("Proxy port must be between 1 and 65535")
    return number


def decode_secret(value: str) -> ProxySecret:
    if not isinstance(value, str) or not value or len(value) > 600:
        raise ValueError("Invalid proxy secret")
    value = value.strip()
    if re.fullmatch(r"[0-9a-fA-F]+", value) and len(value) % 2 == 0:
        raw = bytes.fromhex(value)
    else:
        if not re.fullmatch(r"[A-Za-z0-9_+/=-]+", value):
            raise ValueError("Invalid proxy secret encoding")
        try:
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("Invalid proxy secret encoding") from exc
    if len(raw) == 16:
        return ProxySecret(key=raw, encoded=raw.hex())
    if len(raw) == 17 and raw[0] == 0xDD:
        return ProxySecret(key=raw[1:], padded=True, encoded=raw.hex())
    if 18 <= len(raw) <= 199 and raw[0] == 0xEE:
        try:
            domain = raw[17:].decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid proxy TLS domain") from exc
        normalize_server(domain)
        if domain != domain.strip() or ":" in domain or len(domain) > 182:
            raise ValueError("Invalid proxy TLS domain")
        return ProxySecret(key=raw[1:17], padded=True, domain=domain, encoded=raw.hex())
    raise ValueError("Unsupported or malformed proxy secret")
