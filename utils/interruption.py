"""A cancelled interactive scan can carry only samples actually observed."""

from utils.models import ProxyResult


class ScanInterrupted(KeyboardInterrupt):
    """The pool has drained; results contain completed attempts, including partial retries."""

    def __init__(self, results: list[ProxyResult]):
        super().__init__("Scan interrupted")
        self.results = results
