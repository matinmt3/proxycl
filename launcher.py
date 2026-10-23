"""Small stable launcher for an isolated validated update runtime."""

import subprocess
import sys
from pathlib import Path


def main():
    from utils.updater import UpdateError, _read, runtime_python

    root = Path(__file__).resolve().parent
    try:
        pending = _read(root / ".revmamad" / "updates" / "last.json")
        if pending and pending.get("phase") == "prepared":
            print("An update was interrupted. Run: python main.py update --rollback", file=sys.stderr)
            return 1
        interpreter = (
            runtime_python(root, _read(root / ".revmamad" / "active-runtime.json")) or sys.executable
        )
        return subprocess.call([str(interpreter), str(root / "main.py"), *sys.argv[1:]], cwd=root)
    except (UpdateError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
