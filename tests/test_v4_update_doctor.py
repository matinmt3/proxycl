from __future__ import annotations

import json
import subprocess

import pytest

from utils import updater


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    remote, local = tmp_path / "remote", tmp_path / "installed"
    remote.mkdir()
    git(remote, "init", "-b", "main")
    git(remote, "config", "user.name", "Fixture")
    git(remote, "config", "user.email", "fixture@example.invalid")
    (remote / "main.py").write_text("print('old')\n")
    (remote / "requirements-lite.txt").write_text("")
    (remote / ".gitignore").write_text(".revmamad/\noutput/\n")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "baseline")
    subprocess.run(["git", "clone", str(remote), str(local)], check=True, capture_output=True)
    git(local, "config", "user.name", "Fixture")
    git(local, "config", "user.email", "fixture@example.invalid")
    old = git(local, "rev-parse", "HEAD")
    (remote / "main.py").write_text("print('new')\n")
    git(remote, "add", ".")
    git(remote, "commit", "-m", "candidate")
    new = git(remote, "rev-parse", "HEAD")

    def fake_runtime(root, stage, target):
        assert git(root, "rev-parse", "HEAD") == old
        assert "new" in (stage / "main.py").read_text()
        runtime = root / ".revmamad" / "runtimes" / target / "python"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("fixture executable")
        return {"version": 1, "commit": target, "python": runtime.relative_to(root).as_posix()}

    monkeypatch.setattr(updater, "_prepare_runtime", fake_runtime)
    monkeypatch.setattr(updater, "_verify_runtime", lambda *args: None)
    return local, remote, old, new


def test_update_checks_candidate_before_switch_and_rollback_preserves_user_files(repositories):
    local, _, old, new = repositories
    user = local / ".revmamad" / "user-data"
    user.mkdir(parents=True)
    (user / "sources.json").write_text('{"private": "keep"}')
    (local / "note.txt").write_text("user note")
    assert updater.update(local, check=True)["status"] == "available"
    assert git(local, "rev-parse", "HEAD") == old
    result = updater.update(local)
    assert result["status"] == "updated" and git(local, "rev-parse", "HEAD") == new
    assert not list((local / ".revmamad" / "updates").glob("stage-*"))
    assert updater.runtime_python(local, updater._read(local / ".revmamad" / "active-runtime.json"))
    assert updater.update(local, rollback=True)["status"] == "rolled_back"
    assert git(local, "rev-parse", "HEAD") == old
    assert (user / "sources.json").read_text() == '{"private": "keep"}'
    assert (local / "note.txt").read_text() == "user note"


def test_candidate_failure_does_not_change_installed_code(repositories, monkeypatch):
    local, _, old, _ = repositories

    def fail(*args):
        raise updater.UpdateError("candidate failed")

    monkeypatch.setattr(updater, "_verify_runtime", fail)
    with pytest.raises(updater.UpdateError, match="candidate failed"):
        updater.update(local)
    assert git(local, "rev-parse", "HEAD") == old
    assert "old" in (local / "main.py").read_text()
    assert not (local / ".revmamad" / "active-runtime.json").exists()


def test_dirty_edit_and_later_commit_are_not_discarded(repositories):
    local, _, old, _ = repositories
    (local / "main.py").write_text("local edit")
    with pytest.raises(updater.UpdateError, match="local edits"):
        updater.update(local)
    assert (local / "main.py").read_text() == "local edit"
    git(local, "restore", "main.py")
    updater.update(local)
    (local / "user.py").write_text("user code")
    git(local, "add", ".")
    git(local, "commit", "-m", "user work")
    with pytest.raises(updater.UpdateError, match="discard work"):
        updater.update(local, rollback=True)
    assert (local / "user.py").read_text() == "user code"
    assert git(local, "rev-parse", "HEAD") != old


def test_interrupted_update_requires_recovery_and_restores_before(repositories):
    local, _, old, new = repositories
    updater.update(local)
    path = local / ".revmamad" / "updates" / "last.json"
    record = json.loads(path.read_text())
    record["phase"] = "prepared"
    path.write_text(json.dumps(record))
    with pytest.raises(updater.UpdateError, match="interrupted update"):
        updater.update(local)
    assert git(local, "rev-parse", "HEAD") == new
    updater.update(local, rollback=True)
    assert git(local, "rev-parse", "HEAD") == old


def test_runtime_pointer_and_cleanup_cannot_escape(tmp_path):
    outside = tmp_path / "other.py"
    outside.write_text("keep")
    root = tmp_path / "app"
    root.mkdir()
    with pytest.raises(updater.UpdateError):
        updater.runtime_python(root, {"python": "../other.py"})
    with pytest.raises(updater.UpdateError):
        updater._remove_stage(root, tmp_path)
    assert outside.read_text() == "keep"


def test_rollback_refuses_new_personal_file_at_restored_path(repositories):
    local, remote, _, _ = repositories
    git(remote, "rm", ".gitignore")
    git(remote, "commit", "-m", "remove a tracked path")
    updater.update(local)
    current = git(local, "rev-parse", "HEAD")
    (local / ".gitignore").write_text("personal replacement\n")
    with pytest.raises(updater.UpdateError, match="personal or ignored"):
        updater.update(local, rollback=True)
    assert git(local, "rev-parse", "HEAD") == current
    assert (local / ".gitignore").read_text() == "personal replacement\n"


def test_update_refuses_to_overwrite_ignored_user_file(repositories):
    local, remote, old, _ = repositories
    (remote / "output").mkdir()
    (remote / "output" / "user.json").write_text("candidate")
    git(remote, "add", "-f", "output/user.json")
    git(remote, "commit", "-m", "bad candidate collision")
    (local / "output").mkdir(exist_ok=True)
    (local / "output" / "user.json").write_text("personal")
    with pytest.raises(updater.UpdateError, match="personal or ignored"):
        updater.update(local)
    assert git(local, "rev-parse", "HEAD") == old
    assert (local / "output" / "user.json").read_text() == "personal"


def test_doctor_report_allowlist_never_copies_sensitive_fields(tmp_path):
    from main import load_config
    from utils.doctor import collect_diagnostics

    config = load_config()
    config.output.folder = str(tmp_path)
    mode = tmp_path / "mtproto"
    mode.mkdir()
    secret = "SYNTHETIC_PRIVATE_SENTINEL"
    (mode / "snapshot.json").write_text(
        json.dumps(
            {
                "token": secret,
                "rows": [{"secret": secret}],
                "summary": {"selected": 5, "failure_counts": {secret: 1, "dns_error": 2}, "phone": secret},
            }
        )
    )
    config.sources[0].url = "https://username:" + secret + "@example.invalid/private"
    report = collect_diagnostics(config)
    assert secret not in json.dumps(report)
    assert report["observations"][0]["failures"] == {"dns_error": 2}
    assert report["live_network_tested"] is False and report["account_tested"] is False
    assert report["all_passed"] is True
