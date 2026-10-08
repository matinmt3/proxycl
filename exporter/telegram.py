"""HTTPS Telegram share links, verified by the real MTProto tester."""

from exporter.exporter import Exporter
from utils.models import ProxyResult


class TelegramWebExporter(Exporter):
    link_field = "web_link"
    caption = "Telegram Web Link"

    def connection_link(self, result: ProxyResult) -> str:
        return result.proxy.web_link()

    def export_txt(self, results: list[ProxyResult], filename: str = "proxy.txt"):
        lines = [f"{self.connection_link(r)}  # score={r.score:.3f}" for r in results]
        return self._write(filename, "\n".join(lines) + ("\n" if lines else ""))
