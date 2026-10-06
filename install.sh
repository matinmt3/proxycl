#!/usr/bin/env bash
# Revmamad - one-command Termux installer and interactive launcher.
# Use bash <(curl -fsSL <raw-install-url>) to keep keyboard input available.

set -euo pipefail

REPO_URL="${REVMAMAD_REPO:-https://github.com/matinmt3/proxycl.git}"
REPO_BRANCH="${REVMAMAD_BRANCH:-main}"
REPO_DIR="${REVMAMAD_DIR:-$HOME/Revmamad}"

info() { printf '\n[+] %s\n' "$*"; }
fail() { printf '\n[x] %s\n' "$*" >&2; exit 1; }

command -v pkg >/dev/null 2>&1 || fail "Run this installer inside Termux on Android. For Windows/Linux, follow README.md."
[[ -n "$REPO_URL" && -n "$REPO_BRANCH" && -n "$REPO_DIR" ]] || fail "Repository, branch, and installation directory must not be empty."

printf '\n  REVMAMAD - Telegram MTProto Proxy Radar\n'
info "Updating the Termux package index..."
pkg update -y || fail "Package update failed. Run 'pkg update' in Termux and retry."

info "Installing Python, pip, Git, and build tools..."
pkg install -y python python-pip git clang make pkg-config || fail "Package installation failed. Check the error above and retry."

if [[ -e "$REPO_DIR" ]]; then
    [[ -d "$REPO_DIR/.git" ]] || fail "$REPO_DIR already exists but is not a project checkout. Set REVMAMAD_DIR to another directory."
    CURRENT_ORIGIN="$(git -C "$REPO_DIR" remote get-url origin)" || fail "The existing checkout has no origin remote."
    [[ "${CURRENT_ORIGIN%.git}" == "${REPO_URL%.git}" ]] || fail "Existing checkout uses $CURRENT_ORIGIN. Set REVMAMAD_REPO to that repository or choose another REVMAMAD_DIR."
    CURRENT_BRANCH="$(git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD)" || fail "The existing checkout is not on a branch."
    [[ "$CURRENT_BRANCH" == "$REPO_BRANCH" ]] || fail "Existing checkout is on $CURRENT_BRANCH, expected $REPO_BRANCH. Choose another REVMAMAD_DIR or set REVMAMAD_BRANCH."
    TRACKED_CHANGES="$(git -C "$REPO_DIR" status --porcelain --untracked-files=no)" || fail "Could not inspect the existing checkout."
    [[ -z "$TRACKED_CHANGES" ]] || fail "Existing checkout contains tracked local edits. Commit or save those edits before updating."
    info "Updating the existing checkout..."
    git -C "$REPO_DIR" pull --ff-only origin "$REPO_BRANCH" || fail "Update failed; the application was not launched. Check connectivity and resolve any Git conflict."
else
    info "Cloning $REPO_URL ($REPO_BRANCH)..."
    git clone --depth 1 --single-branch --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR" || fail "Clone failed. Check connectivity and confirm the repository and branch are published."
fi

cd "$REPO_DIR"
if [[ -f main.py && -f requirements-lite.txt ]]; then
    : # Application files are at the repository root.
elif [[ -f TelegramProxySelector/main.py && -f TelegramProxySelector/requirements-lite.txt ]]; then
    cd TelegramProxySelector
else
    fail "Checkout is missing main.py or requirements-lite.txt in the supported project locations."
fi

# Termux manages pip via python-pip; upgrading pip with pip is unsupported.
info "Installing lightweight command-line dependencies..."
python -m pip install -r requirements-lite.txt || fail "Dependency installation failed. Check the package error above; the application was not launched."

info "Installation complete. Next time run:"
printf '    cd %q && python main.py\n' "$PWD"
info "Opening the interactive menu..."
exec python main.py
