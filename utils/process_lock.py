"""Fail-fast, crash-safe ownership of an output root across processes.

The lock file is permanent: deleting a supposedly stale file would allow two
processes to lock different inodes. The operating system releases ownership on
process death. Nested calls on the owning thread are permitted; other threads
and processes must stop before reading/modifying a write transaction.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path


class OutputRootBusy(RuntimeError):
    """Another scan, update or media operation owns this output root."""


_guard = threading.RLock()
_owners: dict[str, tuple[int, int, object, int]] = {}


def output_root_busy(root) -> bool:
    """Inspect current kernel ownership without creating or deleting state."""
    folder = Path(root).expanduser().resolve()
    key = os.path.normcase(str(folder))
    with _guard:
        owner = _owners.get(key)
        if owner is not None and owner[0] == os.getpid():
            return True
        path = folder / ".state" / "writer.lock"
        try:
            stream = path.open("r+b")
        except FileNotFoundError:
            return False
        with stream:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except BlockingIOError:
                return True
            except OSError as exc:
                # Windows CRT maps lock contention to EACCES. Other errors must
                # not misleadingly report that it is safe to write.
                import errno

                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    return True
                raise
        return False


class OutputRootLock:
    def __init__(self, root, *, operation="scan"):
        self.root = Path(root).expanduser().resolve()
        self.operation = operation
        self._key = os.path.normcase(str(self.root))
        self._held = False

    def __enter__(self):
        if self._held:
            raise RuntimeError("Use a separate OutputRootLock instance for nested ownership")
        pid, tid = os.getpid(), threading.get_ident()
        with _guard:
            owner = _owners.get(self._key)
            if owner is not None and owner[0] != pid:
                # An inherited descriptor must not count as ownership after fork.
                owner[2].close()
                _owners.pop(self._key)
                owner = None
            if owner is not None:
                if owner[1] != tid:
                    raise OutputRootBusy("Another operation owns this output directory; stop it first.")
                _owners[self._key] = (*owner[:3], owner[3] + 1)
                self._held = True
                return self
            folder = self.root / ".state"
            folder.mkdir(parents=True, exist_ok=True)
            stream = (folder / "writer.lock").open("a+b")
            try:
                stream.seek(0, os.SEEK_END)
                if stream.tell() == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise OutputRootBusy(
                    "Another operation owns this output directory; stop it first. "
                    "A crashed process releases ownership automatically."
                ) from exc
            _owners[self._key] = (pid, tid, stream, 1)
            self._held = True
            return self

    def __exit__(self, exc_type, exc_value, traceback):
        if not self._held:
            return
        with _guard:
            pid, tid, stream, count = _owners[self._key]
            if (pid, tid) != (os.getpid(), threading.get_ident()):
                raise RuntimeError("Output ownership must be released by the acquiring thread")
            self._held = False
            if count > 1:
                _owners[self._key] = (pid, tid, stream, count - 1)
                return
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()
                del _owners[self._key]
