"""Validate an update in isolation, then fast-forward; preserve user data and rollback.

Never reset dirty tracked files, remove user files, or modify installed system packages.
Each candidate runtime has a permanent path, so venv launchers are not moved.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


class UpdateError(RuntimeError):
    pass


def _run(arguments, *, cwd, timeout=120):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", PIP_DISABLE_PIP_VERSION_CHECK="1")
    try:
        result = subprocess.run(
            [str(value) for value in arguments],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise UpdateError("Update command could not complete; the previous installation is retained") from exc
    if result.returncode:
        # Subprocess output can contain private remote URLs or environment details.
        raise UpdateError(
            f"Update verification failed at {Path(str(arguments[0])).name}; exit {result.returncode}"
        )
    return result.stdout.strip()


def _git(root, *arguments):
    return _run(["git", "-C", root, *arguments], cwd=root)


def _atomic(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path):
    if not path.exists():
        return None
    with path.open("rb") as stream:
        content = stream.read(65537)
    if len(content) > 65536:
        raise UpdateError("Update state is too large")
    try:
        data = json.loads(content)
    except ValueError as exc:
        raise UpdateError("Update state is unreadable; no files were changed") from exc
    if not isinstance(data, dict):
        raise UpdateError("Invalid update state")
    return data


def _managed(root):
    path = root / ".revmamad"
    if path.is_symlink() or (path.exists() and not path.is_dir()) or path.resolve().parent != root:
        raise UpdateError("Managed update directory must be inside this installation")
    path.mkdir(exist_ok=True)
    for name in ("updates", "runtimes"):
        child = path / name
        if child.is_symlink() or child.resolve().parent != path.resolve():
            raise UpdateError("Managed update path cannot be redirected")
        child.mkdir(exist_ok=True)
    return path


def _clean(root):
    if _git(root, "status", "--porcelain", "--untracked-files=no"):
        raise UpdateError(
            "Tracked local edits are preserved. Save them before updating; personal sources belong in user-data"
        )


def _check_collisions(root, target):
    previous = set(_git(root, "ls-files", "-z").split("\0"))
    following = _git(root, "ls-tree", "-r", "--name-only", "-z", target).split("\0")
    for name in filter(None, following):
        if name == ".revmamad" or name.startswith(".revmamad/"):
            raise UpdateError("Candidate release would overwrite managed user data")
        path = root / name
        if name not in previous and (path.exists() or path.is_symlink()):
            raise UpdateError("Candidate release would overwrite a personal or ignored file")
        for parent in path.parents:
            if parent == root:
                break
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise UpdateError("Candidate release crosses a redirected or occupied directory")


def runtime_python(root, pointer):
    if pointer is None:
        return None
    relative = pointer.get("python") if isinstance(pointer, dict) else None
    if not isinstance(relative, str) or "\\" in relative:
        raise UpdateError("Invalid runtime pointer")
    path = root / relative
    base = (root / ".revmamad" / "runtimes").resolve()
    if path.is_symlink() or not path.resolve().is_relative_to(base) or not path.is_file():
        raise UpdateError("Runtime pointer is outside the installation or unavailable")
    return path


def _prepare_runtime(root, stage, target):
    runtime = root / ".revmamad" / "runtimes" / (target[:12] + "-" + uuid4().hex[:8])
    base_python = getattr(sys, "_base_executable", sys.executable) or sys.executable
    # Termux owns pip. Reuse it without upgrading pip or touching system packages.
    _run(
        [base_python, "-m", "venv", "--copies", "--without-pip", "--system-site-packages", runtime], cwd=root
    )
    interpreter = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    _run(
        [
            interpreter,
            "-m",
            "pip",
            "install",
            "--no-input",
            "--timeout",
            "30",
            "--retries",
            "2",
            "-r",
            stage / "requirements-lite.txt",
        ],
        cwd=stage,
        timeout=600,
    )
    return {"version": 1, "commit": target, "python": interpreter.relative_to(root).as_posix()}


def _verify_runtime(root, stage, pointer):
    interpreter = runtime_python(root, pointer)
    _run(
        [
            interpreter,
            "-c",
            "import main,httpx,yaml,idna; from Crypto.Cipher import AES; AES.new(bytes(16),AES.MODE_ECB)",
        ],
        cwd=stage,
    )
    _run([interpreter, "main.py", "selftest", "--network", "update-validation"], cwd=stage, timeout=120)


def _remove_stage(root, stage):
    base = (root / ".revmamad" / "updates").resolve()
    actual = stage.resolve()
    if stage.is_symlink() or actual.parent != base or not stage.name.startswith("stage-"):
        raise UpdateError("Refusing cleanup outside this update's staging directory")
    if stage.exists():
        shutil.rmtree(stage)


def _record_valid(record):
    return (
        isinstance(record, dict)
        and record.get("schema_version") == 1
        and all(
            isinstance(record.get(key), str) and re.fullmatch(r"[0-9a-f]{40,64}", record[key])
            for key in ("before", "after")
        )
        and record.get("phase") in {"prepared", "active", "rolled_back", "failed"}
    )


def update(root: Path, *, rollback=False, check=False, ref=None, output_folder=None) -> dict:
    from utils.process_lock import OutputRootLock

    root = root.resolve()
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise UpdateError("Run updates from the repository root installation")
    managed = _managed(root)
    # The output lock coordinates scanners; installation ownership also prevents
    # two configurations with different output paths from updating the same code.
    with (
        OutputRootLock(managed / "updates", operation="update"),
        OutputRootLock(output_folder or root / "output", operation="update"),
    ):
        _clean(root)
        record_path = managed / "updates" / "last.json"
        pointer_path = managed / "active-runtime.json"
        before = _git(root, "rev-parse", "HEAD")
        old_record = _read(record_path)
        if rollback:
            if not _record_valid(old_record) or old_record["phase"] not in {"prepared", "active"}:
                raise UpdateError("No pending or active update is available to roll back")
            if before not in (old_record["after"], old_record["before"]):
                raise UpdateError(
                    "Repository changed after the update; automatic rollback would discard work"
                )
            previous = old_record.get("previous_runtime")
            runtime_python(root, previous)
            if before == old_record["after"]:
                _check_collisions(root, old_record["before"])
                _git(root, "reset", "--keep", old_record["before"])
            if previous is None:
                pointer_path.unlink(missing_ok=True)
            else:
                _atomic(pointer_path, previous)
            old_record["phase"] = "rolled_back"
            _atomic(record_path, old_record)
            return {"status": "rolled_back", "commit": old_record["before"], "user_data_preserved": True}
        # Recovery only needs the recorded previous runtime. Requiring the
        # failed candidate or its pointer to work would make rollback unusable.
        pointer = _read(pointer_path)
        runtime_python(root, pointer)
        if old_record and old_record.get("phase") == "prepared":
            raise UpdateError("An interrupted update needs `update --rollback` before another update")
        branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        wanted = ref or branch
        if (
            not isinstance(wanted, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", wanted)
            or ".." in wanted
        ):
            raise UpdateError("Choose a literal branch or tag")
        _git(root, "fetch", "--", "origin", wanted)
        after = _git(root, "rev-parse", "FETCH_HEAD^{commit}")
        if before == after:
            return {"status": "up_to_date", "commit": before}
        _git(root, "merge-base", "--is-ancestor", before, after)
        _check_collisions(root, after)
        if check:
            return {"status": "available", "before": before, "after": after}
        stage = Path(tempfile.mkdtemp(prefix="stage-", dir=managed / "updates"))
        try:
            _git(root, "clone", "--no-checkout", "--no-hardlinks", "--", str(root), str(stage))
            _git(stage, "checkout", "--detach", after)
            if not (stage / "main.py").is_file() or not (stage / "requirements-lite.txt").is_file():
                raise UpdateError("Candidate is missing application files")
            candidate = _prepare_runtime(root, stage, after)
            _verify_runtime(root, stage, candidate)
            _clean(root)
            _check_collisions(root, after)
            if _git(root, "rev-parse", "HEAD") != before:
                raise UpdateError("Repository changed while preparing the update")
            record = {
                "schema_version": 1,
                "phase": "prepared",
                "before": before,
                "after": after,
                "previous_runtime": pointer,
                "candidate_runtime": candidate,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            _atomic(record_path, record)
            try:
                _git(root, "merge", "--ff-only", "--no-edit", after)
                _atomic(pointer_path, candidate)
                record["phase"] = "active"
                _atomic(record_path, record)
            except (UpdateError, OSError):
                # --keep refuses conflicting edits and preserves untracked files.
                _clean(root)
                if _git(root, "rev-parse", "HEAD") == after:
                    _check_collisions(root, before)
                    _git(root, "reset", "--keep", before)
                if pointer is None:
                    pointer_path.unlink(missing_ok=True)
                else:
                    _atomic(pointer_path, pointer)
                record["phase"] = "failed"
                _atomic(record_path, record)
                raise
            return {
                "status": "updated",
                "commit": after,
                "user_data_preserved": True,
                "restart_required": True,
            }
        finally:
            _remove_stage(root, stage)
