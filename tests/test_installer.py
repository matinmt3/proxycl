"""Offline installer regressions. No packages or remote Git operations are performed."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[1] / "install.sh"
DEFAULT_REPO = "https://github.com/matinmt3/proxycl.git"


def shell_path(path: Path) -> str:
    value = path.resolve().as_posix()
    if os.name == "nt" and len(value) > 2 and value[1:3] == ":/":
        return "/" + value[0].lower() + value[2:]
    return value


COMMON = """#!/usr/bin/env bash
set -eu
printf '%s\\t' "$(basename "$0")" "$@" >> "$MOCK_LOG"
printf '\\n' >> "$MOCK_LOG"
"""
MOCKS = {
    "pkg": COMMON
    + """
[[ "${MOCK_FAIL:-}" != "pkg_$1" ]] || exit 21
case "$1" in update|install) : ;; *) exit 90 ;; esac
if [[ "${MOCK_CREATE_TARGET:-}" == 1 && "$1" == install ]]; then
    mkdir -p "$MOCK_TARGET"
    printf 'keep' > "$MOCK_TARGET/user-note.txt"
fi
""",
    "git": COMMON
    + """
while [[ "$1" == -c ]]; do shift 2; done
if [[ "$1" == check-ref-format ]]; then
    [[ "$2" == --branch && -n "$3" && "$3" != -* && "$3" != *..* ]] || exit 22
    exit 0
fi
if [[ "$1" == clone ]]; then
    [[ "${GIT_TERMINAL_PROMPT:-}" == 0 ]] || exit 91
    shift
    branch=''
    while [[ "$1" == --* ]]; do
        case "$1" in
            --depth) shift 2 ;;
            --single-branch) shift ;;
            --branch) branch="$2"; shift 2 ;;
            --) shift; break ;;
            *) exit 92 ;;
        esac
    done
    [[ $# == 2 && -n "$branch" ]] || exit 93
    repo="$1"; destination="$2"
    mkdir -p -- "$destination/.git"
    printf '%s\\n' "$repo" > "$destination/.test-origin"
    printf '%s\\n' "$branch" > "$destination/.test-branch"
    [[ "${MOCK_FAIL:-}" != clone ]] || { touch -- "$destination/partial-download"; exit 23; }
    case "${MOCK_LAYOUT:-root}" in
        root) touch -- "$destination/main.py" "$destination/requirements-lite.txt" ;;
        nested) mkdir -p -- "$destination/TelegramProxySelector"; touch -- "$destination/TelegramProxySelector/main.py" "$destination/TelegramProxySelector/requirements-lite.txt" ;;
        missing) touch -- "$destination/README.md" ;;
        *) exit 94 ;;
    esac
else
    [[ "$1" == -C ]] || exit 95
    checkout="$2"; shift 2
    case "$1" in
        remote) cat "$checkout/.test-origin" ;;
        symbolic-ref) [[ -s "$checkout/.test-branch" ]] || exit 24; cat "$checkout/.test-branch" ;;
        status) [[ ! -f "$checkout/.test-dirty" ]] || cat "$checkout/.test-dirty" ;;
        pull) [[ "${GIT_TERMINAL_PROMPT:-}" == 0 ]] || exit 96; [[ "${MOCK_FAIL:-}" != pull ]] || exit 25 ;;
        *) exit 97 ;;
    esac
fi
""",
    "python": COMMON
    + """
if [[ "$1" == -m && "$2" == pip ]]; then
    [[ "$3" == install && -f requirements-lite.txt ]] || exit 98
    [[ "${MOCK_FAIL:-}" != pip ]] || exit 26
elif [[ "$1" == -c ]]; then
    [[ -f main.py ]] || exit 99
    [[ "${MOCK_FAIL:-}" != runtime ]] || exit 27
elif [[ "$*" == main.py ]]; then
    [[ -f main.py ]] || exit 100
    printf 'launch-cwd\\t%s\\n' "$PWD" >> "$MOCK_LOG"
    if command -v cygpath >/dev/null 2>&1; then
        printf 'launch-cwd-native\\t%s\\n' "$(cygpath -am -- "$PWD")" >> "$MOCK_LOG"
    fi
    IFS= read -r answer || exit 101
    printf 'menu-input\\t%s\\n' "$answer" >> "$MOCK_LOG"
    exit "${MOCK_APP_EXIT:-0}"
else
    exit 102
fi
""",
}


class OfflineInstall:
    def __init__(self, root: Path, bash: str):
        self.root = root
        self.bash = bash
        self.home = root / "home with spaces"
        self.home.mkdir()
        self.cwd = root / "different invocation directory"
        self.cwd.mkdir()
        self.bin = root / "mock bin"
        self.bin.mkdir()
        self.log = root / "commands.tsv"
        self.target = self.home / "Revmamad"
        self.repo = DEFAULT_REPO
        self.branch = "main"
        self.options = {}
        for name, source in MOCKS.items():
            file = self.bin / name
            file.write_text(source, encoding="utf-8", newline="\n")
            file.chmod(0o755)

    def checkout(self, *, layout="root", gitfile=False):
        self.target.mkdir(parents=True)
        if gitfile:
            (self.target / ".git").write_text("gitdir: fixture", encoding="utf-8")
        else:
            (self.target / ".git").mkdir()
        (self.target / ".test-origin").write_text(self.repo + "\n", encoding="utf-8")
        (self.target / ".test-branch").write_text(self.branch + "\n", encoding="utf-8")
        (self.target / "user-note.txt").write_text("keep", encoding="utf-8")
        self.app_files(layout)

    def app_files(self, layout):
        app = self.target / "TelegramProxySelector" if layout == "nested" else self.target
        app.mkdir(exist_ok=True)
        if layout != "missing":
            (app / "main.py").touch()
            (app / "requirements-lite.txt").touch()

    def run(self, *, failure="", layout="root", app_exit=0, create_target=False):
        self.log.write_text("", encoding="utf-8")
        env = os.environ.copy()
        for name in ("REVMAMAD_REPO", "REVMAMAD_BRANCH", "REVMAMAD_DIR"):
            env.pop(name, None)
        env.update(
            {
                "HOME": shell_path(self.home),
                "MOCK_BIN": shell_path(self.bin),
                "INSTALLER": shell_path(INSTALLER),
                "MOCK_LOG": shell_path(self.log),
                "MOCK_FAIL": failure,
                "MOCK_LAYOUT": layout,
                "MOCK_APP_EXIT": str(app_exit),
                "MOCK_CREATE_TARGET": "1" if create_target else "0",
                "MOCK_TARGET": shell_path(self.target),
            }
        )
        env.update(self.options)
        # Git for Windows' launcher prepends its real git.exe directory. Reset PATH
        # inside Bash and refuse to proceed unless all substitutes resolve first.
        command = (
            'export PATH="$MOCK_BIN:/usr/bin:/bin"; hash -r; '
            'for tool in git python; do [[ "$(command -v "$tool")" == "$MOCK_BIN/$tool" ]] || exit 103; done; '
            'bash <(cat "$INSTALLER")'
        )
        self.result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "-c", command],
            cwd=self.cwd,
            env=env,
            input="5\n",
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
        )
        self.events = [
            line.rstrip("\t").split("\t") for line in self.log.read_text(encoding="utf-8").splitlines()
        ]
        assert self.result.returncode != 103, "Test substitutes were bypassed"
        return self.result

    def calls(self, command, action):
        result = []
        for event in self.events:
            if event[0] != command:
                continue
            arguments = event[1:]
            if command == "git":
                while arguments and arguments[0] == "-c":
                    arguments = arguments[2:]
                if arguments and arguments[0] == "-C":
                    arguments = arguments[2:]
            if arguments and arguments[0] == action:
                result.append(arguments)
        return result

    def assert_launched(self, expected_exit=0, layout="root"):
        assert self.result.returncode == expected_exit, self.result.stderr
        assert self.calls("python", "main.py") == [["main.py"]]
        assert ["menu-input", "5"] in self.events
        app = self.target / "TelegramProxySelector" if layout == "nested" else self.target
        # Git Bash mounts Windows TEMP as /tmp. A relative cd can retain that
        # alias rather than /c/...; compare filesystem identity, not spelling.
        cwd_event = "launch-cwd-native" if os.name == "nt" else "launch-cwd"
        launch_directories = [event[1] for event in self.events if event[0] == cwd_event]
        assert len(launch_directories) == 1
        assert Path(launch_directories[0]).samefile(app), (launch_directories[0], str(app))
        assert not (self.cwd / "Revmamad").exists()
        assert not list(self.target.parent.glob(self.target.name + ".install.*"))


@pytest.fixture
def offline_install(tmp_path):
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    bash = str(git_bash) if os.name == "nt" and git_bash.is_file() else shutil.which("bash")
    if not bash:
        pytest.skip("Bash is required for offline installer regression checks")
    return OfflineInstall(tmp_path, bash)


@pytest.mark.parametrize("layout", ["root", "nested"])
def test_fresh_install_and_keyboard_input(offline_install, layout):
    run = offline_install
    run.run(layout=layout)
    run.assert_launched(layout=layout)
    clone = run.calls("git", "clone")[0]
    assert clone[-2] == DEFAULT_REPO
    assert clone[-1].startswith(shell_path(run.target) + ".install.")
    assert "--" in clone
    pip = run.calls("python", "-m")[0]
    assert "--no-input" in pip and "--timeout" in pip and "--retries" in pip
    assert pip[-2:] == ["-r", "requirements-lite.txt"]
    runtime_check = run.calls("python", "-c")
    assert len(runtime_check) == 1 and "AES.new" in runtime_check[0][1]


@pytest.mark.parametrize("gitfile", [False, True])
@pytest.mark.parametrize("layout", ["root", "nested"])
def test_existing_checkout_updates_without_losing_files(offline_install, gitfile, layout):
    run = offline_install
    run.checkout(layout=layout, gitfile=gitfile)
    run.run(layout=layout)
    run.assert_launched(layout=layout)
    assert not run.calls("git", "clone")
    assert run.calls("git", "pull") == [["pull", "--ff-only", "--", "origin", "main"]]
    assert (run.target / "user-note.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize(
    "kind", ["file", "directory", "wrong_origin", "wrong_branch", "dirty", "missing_origin", "detached"]
)
def test_conflicting_checkout_fails_before_package_changes(offline_install, kind):
    run = offline_install
    if kind == "file":
        run.target.write_text("keep", encoding="utf-8")
    elif kind == "directory":
        run.target.mkdir()
    else:
        run.checkout()
        if kind == "wrong_origin":
            (run.target / ".test-origin").write_text("https://github.com/other/project.git", encoding="utf-8")
        elif kind == "wrong_branch":
            (run.target / ".test-branch").write_text("other", encoding="utf-8")
        elif kind == "dirty":
            (run.target / ".test-dirty").write_text(" M config/config.yaml", encoding="utf-8")
        elif kind == "missing_origin":
            (run.target / ".test-origin").unlink()
        elif kind == "detached":
            (run.target / ".test-branch").write_text("", encoding="utf-8")
    run.run()
    assert run.result.returncode == 1
    assert not run.calls("pkg", "update") and not run.calls("pkg", "install")
    assert not run.calls("git", "pull") and not run.calls("git", "clone")
    assert not run.calls("python", "main.py")


@pytest.mark.parametrize("failure", ["pkg_update", "pkg_install", "clone", "pull", "pip", "runtime"])
def test_failed_install_stage_never_launches_menu(offline_install, failure):
    run = offline_install
    if failure == "pull":
        run.checkout()
    run.run(failure=failure)
    assert run.result.returncode == 1
    assert not run.calls("python", "main.py")
    assert not list(run.target.parent.glob(run.target.name + ".install.*"))
    if failure == "clone":
        assert not run.target.exists()


def test_failed_partial_clone_can_be_retried(offline_install):
    run = offline_install
    run.run(failure="clone")
    assert run.result.returncode == 1 and not run.target.exists()
    run.run()
    run.assert_launched()


def test_repository_branch_and_directory_overrides(offline_install):
    run = offline_install
    run.target = run.root / "new parent" / "custom target with spaces"
    run.repo = "https://github.com/fixture/project.git"
    run.branch = "feature/mobile-v2"
    run.options = {
        "REVMAMAD_DIR": shell_path(run.target),
        "REVMAMAD_REPO": run.repo,
        "REVMAMAD_BRANCH": run.branch,
    }
    run.run()
    run.assert_launched()
    clone = run.calls("git", "clone")[0]
    assert clone[-2] == run.repo and clone[clone.index("--branch") + 1] == run.branch


@pytest.mark.parametrize("suffix", ["/", "///"])
def test_directory_override_allows_trailing_slashes(offline_install, suffix):
    run = offline_install
    run.options["REVMAMAD_DIR"] = shell_path(run.target) + suffix
    run.run()
    run.assert_launched()


def test_directory_override_keeps_shell_characters_literal(offline_install):
    run = offline_install
    run.target = run.root / "target $literal (with parentheses)"
    run.options["REVMAMAD_DIR"] = shell_path(run.target)
    run.run()
    run.assert_launched()


def test_relative_directory_starting_with_dash_is_not_an_option(offline_install):
    run = offline_install
    run.target = run.cwd / "-custom-directory"
    run.options["REVMAMAD_DIR"] = "-custom-directory"
    run.run()
    run.assert_launched()


@pytest.mark.parametrize("variable", ["REVMAMAD_DIR", "REVMAMAD_REPO", "REVMAMAD_BRANCH"])
def test_empty_override_has_a_clear_error(offline_install, variable):
    run = offline_install
    run.options[variable] = ""
    run.run()
    assert run.result.returncode == 1
    assert "must not be empty" in run.result.stderr
    assert not run.events


def test_application_exit_status_is_preserved(offline_install):
    run = offline_install
    run.run(app_exit=37)
    run.assert_launched(expected_exit=37)


def test_missing_application_files_are_rejected(offline_install):
    run = offline_install
    run.run(layout="missing")
    assert run.result.returncode == 1
    assert "missing main.py" in run.result.stderr
    assert not run.calls("python", "main.py")


def test_new_directory_during_packages_is_not_touched(offline_install):
    run = offline_install
    run.run(create_target=True)
    assert run.result.returncode == 1
    assert not run.calls("git", "pull") and not run.calls("git", "clone")
    assert (run.target / "user-note.txt").read_text(encoding="utf-8") == "keep"


def test_platform_guard_runs_before_other_commands(offline_install):
    run = offline_install
    (run.bin / "pkg").unlink()
    run.run()
    assert run.result.returncode == 1
    assert "Termux" in run.result.stderr and not run.events
