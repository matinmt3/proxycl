#!/usr/bin/env bash
# Revmamad - one-command Termux installer and interactive launcher.
# Use bash <(curl -fsSL <raw-install-url>) to keep keyboard input available.

set -euo pipefail

REPO_URL="${REVMAMAD_REPO-https://github.com/matinmt3/proxycl.git}"
REPO_BRANCH="${REVMAMAD_BRANCH-main}"
REPO_DIR="${REVMAMAD_DIR-${HOME:-}/Revmamad}"
CLONE_DIR=""
EXISTING_CHECKOUT=false

info() { printf '\n[+] %s\n' "$*"; }
fail() { printf '\n[x] %s\n' "$*" >&2; exit 1; }

cleanup() {
    # Only remove the temporary directory created by this installer.
    if [[ -n "$CLONE_DIR" && -d "$CLONE_DIR" ]]; then
        rm -rf -- "$CLONE_DIR"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

git_network() {
    # A public install must not consume the menu's keyboard input asking for Git credentials.
    GIT_TERMINAL_PROMPT=0 git -c http.lowSpeedLimit=1 -c http.lowSpeedTime=30 "$@"
}

command -v pkg >/dev/null 2>&1 || fail "Run this installer inside Termux on Android. For Windows/Linux, follow README.md."
[[ -n "$REPO_URL" && -n "$REPO_BRANCH" && -n "$REPO_DIR" ]] || fail "Repository, branch, and installation directory must not be empty."
[[ -n "${HOME:-}" || -n "${REVMAMAD_DIR:-}" ]] || fail "HOME is unavailable. Set REVMAMAD_DIR to an installation directory."
while [[ "$REPO_DIR" == */ && "$REPO_DIR" != / ]]; do
    REPO_DIR="${REPO_DIR%/}"
done

printf '\n  REVMAMAD - Telegram MTProto Proxy Radar\n'
if [[ -e "$REPO_DIR" || -L "$REPO_DIR" ]]; then
    [[ -e "$REPO_DIR/.git" ]] || fail "$REPO_DIR already exists but is not a project checkout. Set REVMAMAD_DIR to another directory."
    command -v git >/dev/null 2>&1 || fail "Git is unavailable. Run 'pkg install git' in Termux and retry."
    CURRENT_ORIGIN="$(git -C "$REPO_DIR" remote get-url origin)" || fail "The existing checkout has no origin remote."
    [[ "${CURRENT_ORIGIN%.git}" == "${REPO_URL%.git}" ]] || fail "Existing checkout uses $CURRENT_ORIGIN. Set REVMAMAD_REPO to that repository or choose another REVMAMAD_DIR."
    CURRENT_BRANCH="$(git -C "$REPO_DIR" symbolic-ref --quiet --short HEAD)" || fail "The existing checkout is not on a branch."
    [[ "$CURRENT_BRANCH" == "$REPO_BRANCH" ]] || fail "Existing checkout is on $CURRENT_BRANCH, expected $REPO_BRANCH. Choose another REVMAMAD_DIR or set REVMAMAD_BRANCH."
    TRACKED_CHANGES="$(git -C "$REPO_DIR" status --porcelain --untracked-files=no)" || fail "Could not inspect the existing checkout."
    [[ -z "$TRACKED_CHANGES" ]] || fail "Existing checkout contains tracked local edits. Commit or save those edits before updating."
    EXISTING_CHECKOUT=true
fi

info "Updating the Termux package index..."
pkg update -y || fail "Package update failed. Run 'pkg update' in Termux; if the mirror is unreachable, use 'termux-change-repo' and retry."

info "Installing Python, pip, Git, and build tools..."
pkg install -y python python-pip git clang make pkg-config || fail "Package installation failed. Check the error above and retry."
git check-ref-format --branch "$REPO_BRANCH" >/dev/null || fail "Invalid Git branch name: $REPO_BRANCH"

if "$EXISTING_CHECKOUT"; then
    info "Updating the existing checkout..."
    git_network -C "$REPO_DIR" pull --ff-only -- origin "$REPO_BRANCH" || fail "Update failed; the application was not launched. Check connectivity and resolve any Git conflict."
else
    [[ ! -e "$REPO_DIR" && ! -L "$REPO_DIR" ]] || fail "Installation directory appeared while preparing packages. Rerun to inspect that checkout."
    info "Cloning $REPO_URL ($REPO_BRANCH)..."
    mkdir -p -- "$(dirname -- "$REPO_DIR")" || fail "Could not create the installation parent directory. Check storage space and permissions."
    CLONE_DIR="$(mktemp -d -- "${REPO_DIR}.install.XXXXXX")" || fail "Could not create an installation directory. Check storage space and permissions."
    git_network clone --depth 1 --single-branch --branch "$REPO_BRANCH" -- "$REPO_URL" "$CLONE_DIR" || fail "Clone failed. Check connectivity and confirm the repository and branch are published; rerunning is safe."
    [[ ! -e "$REPO_DIR" && ! -L "$REPO_DIR" ]] || fail "Installation directory appeared while cloning. Rerun to inspect that checkout."
    mv -T -- "$CLONE_DIR" "$REPO_DIR" || fail "Could not finish the installation. Check storage space and permissions."
    CLONE_DIR=""
fi

cd -- "$REPO_DIR"
if [[ -f main.py && -f requirements-lite.txt ]]; then
    : # Application files are at the repository root.
elif [[ -f TelegramProxySelector/main.py && -f TelegramProxySelector/requirements-lite.txt ]]; then
    cd TelegramProxySelector
else
    fail "Checkout is missing main.py or requirements-lite.txt in the supported project locations."
fi

# Termux manages pip via python-pip; upgrading pip with pip is unsupported.
info "Installing lightweight command-line dependencies..."
python -m pip install --no-input --disable-pip-version-check --timeout 30 --retries 2 -r requirements-lite.txt || fail "Dependency installation failed. Check connectivity to PyPI and the package error above; rerun the installer after fixing it."

info "Checking command-line dependencies..."
python -c 'import main, httpx, yaml, tqdm; from Crypto.Cipher import AES; AES.new(bytes(16), AES.MODE_ECB).encrypt(bytes(16))' || fail "The command-line dependencies could not load. Check the error above for a missing package or a conflicting Crypto installation."

info "Installation complete. Next time run:"
printf '    cd %q && python main.py\n' "$PWD"
info "Opening the interactive menu..."
exec python main.py
