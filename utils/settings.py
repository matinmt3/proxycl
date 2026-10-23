"""Save validated configuration separately from shipped files, with a backup."""

import os
import tempfile
from dataclasses import asdict
from pathlib import Path


def save_settings(config):
    import yaml

    from config.loader import AppConfig
    from utils.process_lock import OutputRootLock
    from utils.user_sources import data_dir

    folder = data_dir(config)
    path = folder / "settings.yaml"
    document = asdict(config)
    document.pop("_path", None)
    # All paths in a loaded AppConfig are absolute; a relocated settings file
    # must not silently change the origin of a relative source/output path.
    body = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    if len(body.encode("utf-8")) > 4 * 1024 * 1024:
        raise ValueError("Personal configuration exceeds 4 MiB")
    with OutputRootLock(config.output.folder, operation="settings"):
        folder.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".yaml", dir=folder, delete=False
            ) as stream:
                temporary = Path(stream.name)
                os.chmod(temporary, 0o600)
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            AppConfig.load(temporary)
            if path.exists():
                backup = path.with_suffix(".backup.yaml")
                with backup.open("wb") as stream:
                    stream.write(path.read_bytes())
                os.chmod(backup, 0o600)
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
    return path
