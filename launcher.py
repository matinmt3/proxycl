"""Small stable launcher for an isolated validated update runtime."""

import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    try:
        # main owns runtime selection after parsing the command. In particular,
        # update --rollback must run even if the candidate runtime is unusable.
        return subprocess.call([sys.executable, str(root / "main.py"), *sys.argv[1:]], cwd=root)
    except OSError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
