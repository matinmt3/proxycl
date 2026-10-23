"""Run the candidate updater against an older installation without replacing it first."""

import json
import sys
from pathlib import Path


def main():
    from config.loader import AppConfig
    from utils.updater import UpdateError, update

    if len(sys.argv) != 3:
        print("Expected installation path and branch", file=sys.stderr)
        return 1
    root = Path(sys.argv[1]).resolve()
    config_file = root / ".revmamad" / "user-data" / "settings.yaml"
    if not config_file.is_file():
        config_file = root / "config" / "config.yaml"
    try:
        config = AppConfig.load(config_file)
        print(json.dumps(update(root, ref=sys.argv[2], output_folder=config.output.folder), indent=2))
    except (UpdateError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
