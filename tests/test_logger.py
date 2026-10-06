import logging
import uuid
from pathlib import Path

from utils.logger import ColorFormatter, setup_logger


def test_unwritable_log_location_keeps_console_running(tmp_path, capsys):
    blocked = tmp_path / "blocked"
    blocked.write_text("regular file", encoding="utf-8")
    logger = setup_logger("test-" + uuid.uuid4().hex, str(blocked / "logs"))
    try:
        logger.info("still working")
        output = capsys.readouterr().out
        assert "File logging unavailable" in output and "still working" in output
        assert "\033[" not in output
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_existing_logger_updates_requested_level(tmp_path):
    name = "test-" + uuid.uuid4().hex
    logger = setup_logger(name, str(tmp_path), logging.WARNING)
    try:
        assert setup_logger(name, str(tmp_path), logging.DEBUG).level == logging.DEBUG
        assert len(logger.handlers) == 2
        assert (Path(tmp_path) / (name + ".log")).exists()
    finally:
        for handler in list(logger.handlers):
            handler.close()
            logger.removeHandler(handler)


def test_formatter_can_disable_terminal_escape_codes():
    record = logging.LogRecord("test", logging.INFO, "", 0, "hello", (), None)
    assert ColorFormatter("%(message)s", color=False).format(record) == "hello"
