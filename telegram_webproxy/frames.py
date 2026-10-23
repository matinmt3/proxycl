"""Bounded v1 shared-frame codec, independently written from the wire contract."""

from __future__ import annotations

from dataclasses import dataclass

OPEN, DATA, CLOSE, WINDOW, PING, PONG = 1, 2, 3, 4, 5, 6
HELLO, WELCOME, BYE = 0x10, 0x11, 0x1F
INITIAL_WINDOW = 4 * 1024 * 1024
DATA_CHUNK = 64 * 1024
MAX_PAYLOAD = 1024 * 1024
MAX_BATCH = 4 * 1024 * 1024
MAX_FRAMES = 4096


@dataclass(frozen=True)
class Frame:
    type: int
    stream: int
    payload: bytes = b""


def encode(kind: int, stream: int = 0, payload: bytes = b"") -> bytes:
    if (
        type(kind) is not int
        or not 0 <= kind <= 255
        or type(stream) is not int
        or not 0 <= stream <= 0xFFFFFF
    ):
        raise ValueError("Invalid WEB frame header")
    if not isinstance(payload, bytes) or len(payload) > MAX_PAYLOAD:
        raise ValueError("Invalid WEB frame payload")
    return bytes([kind]) + stream.to_bytes(3, "big") + len(payload).to_bytes(4, "big") + payload


def decode(batch: bytes, *, server: bool = True, welcome: bool = False) -> list[Frame]:
    if not isinstance(batch, bytes) or not 8 <= len(batch) <= MAX_BATCH:
        raise ValueError("Invalid WEB frame batch")
    result, offset = [], 0
    while offset < len(batch):
        if len(result) >= MAX_FRAMES or len(batch) - offset < 8:
            raise ValueError("Invalid WEB frame batch")
        kind, stream = batch[offset], int.from_bytes(batch[offset + 1 : offset + 4], "big")
        size = int.from_bytes(batch[offset + 4 : offset + 8], "big")
        offset += 8
        if size > MAX_PAYLOAD or offset + size > len(batch):
            raise ValueError("Truncated or oversized WEB frame")
        payload = batch[offset : offset + size]
        offset += size
        if stream == 0:
            allowed = (WELCOME, PING, BYE) if server else (HELLO, PONG)
            if (
                kind not in allowed
                or (kind == WELCOME and payload)
                or (kind in (PING, PONG, BYE) and size > 64)
            ):
                raise ValueError("Invalid WEB control frame")
            if kind == HELLO and payload != b"\x01":
                raise ValueError("Invalid WEB HELLO")
        elif kind in (OPEN, CLOSE):
            if payload or (server and kind == OPEN):
                raise ValueError("Invalid WEB stream control")
        elif kind == DATA:
            # Relay socket reads are 64 KiB but adjacent queued DATA frames may
            # coalesce up to MaxFramePayload (session.go queue admission).
            if not payload:
                raise ValueError("Invalid WEB DATA")
        elif kind == WINDOW:
            if size != 4 or not int.from_bytes(payload, "big"):
                raise ValueError("Invalid WEB WINDOW")
        else:
            raise ValueError("Unknown WEB frame")
        result.append(Frame(kind, stream, payload))
    if welcome and result != [Frame(WELCOME, 0)]:
        raise ValueError("Expected one WEB WELCOME")
    if not welcome and any(item.type == WELCOME for item in result):
        raise ValueError("Repeated WEB WELCOME")
    return result
