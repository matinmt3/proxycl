"""Official WEB links use the direct scheme for compatible Telegram clients."""

from exporter.exporter import Exporter


class TelegramWebProxyExporter(Exporter):
    caption = "Telegram WEB"

    def export_txt(self, results, filename="proxy.txt"):
        return self._write(filename, "".join(self.connection_link(row) + "\n" for row in results))
