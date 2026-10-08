"""Web proxy candidates with explicit transport identity."""

from __future__ import annotations

from dataclasses import dataclass

from tester.secrets import normalize_port, normalize_server

PROTOCOLS = frozenset({"http", "https", "socks4", "socks5"})


@dataclass
class WebProxy:
    server: str
    port: int
    protocol: str = "http"
    source: str = "unknown"

    def __post_init__(self):
        self.server = normalize_server(self.server)
        self.port = normalize_port(self.port)
        if not isinstance(self.protocol, str) or self.protocol.strip().lower() not in PROTOCOLS:
            raise ValueError("Unsupported web proxy transport")
        self.protocol = self.protocol.strip().lower()

    def uri(self) -> str:
        host = f"[{self.server}]" if ":" in self.server else self.server
        return f"{self.protocol}://{host}:{self.port}"

    def key(self) -> str:
        return self.uri()

    def to_dict(self) -> dict:
        return {
            "server": self.server,
            "port": self.port,
            "protocol": self.protocol,
            "source": self.source,
            "uri": self.uri(),
        }
