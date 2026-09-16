"""setup_logging() — root logger configuration, rotating file handler,
and the "safe to call more than once" guarantee its docstring promises."""

from __future__ import annotations

import logging
import logging.handlers

from naukri_agent.config import Settings
from naukri_agent.logging_config import setup_logging


def _settings(tmp_path, **over) -> Settings:
    base = dict(_env_file=None, log_dir=str(tmp_path / "logs"), log_level="INFO")
    base.update(over)
    return Settings(**base)


def teardown_function():
    # setup_logging() mutates the process-wide root logger; leave it
    # clean for whichever test (in this file or another) runs next.
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.WARNING)


def test_creates_log_dir_and_file(tmp_path):
    settings = _settings(tmp_path)
    setup_logging(settings)

    log_file = tmp_path / "logs" / "naukri_agent.log"
    assert log_file.parent.is_dir()

    logging.getLogger("naukri_agent.test").info("hello")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert log_file.exists()
    assert "hello" in log_file.read_text(encoding="utf-8")


def test_sets_root_level_from_settings(tmp_path):
    setup_logging(_settings(tmp_path, log_level="WARNING"))
    assert logging.getLogger().level == logging.WARNING

    setup_logging(_settings(tmp_path, log_level="DEBUG"))
    assert logging.getLogger().level == logging.DEBUG


def test_attaches_console_and_rotating_file_handlers(tmp_path):
    setup_logging(_settings(tmp_path))
    handlers = logging.getLogger().handlers

    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
        for h in handlers
    )
    file_handlers = [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(file_handlers) == 1
    assert file_handlers[0].maxBytes == 5 * 1024 * 1024
    assert file_handlers[0].backupCount == 5


def test_calling_twice_does_not_duplicate_handlers(tmp_path):
    settings = _settings(tmp_path)
    setup_logging(settings)
    setup_logging(settings)

    assert len(logging.getLogger().handlers) == 2


def test_log_lines_are_not_duplicated_across_setup_calls(tmp_path):
    settings = _settings(tmp_path)
    setup_logging(settings)
    setup_logging(settings)

    logging.getLogger("naukri_agent.test").info("only once")
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_file = tmp_path / "logs" / "naukri_agent.log"
    assert log_file.read_text(encoding="utf-8").count("only once") == 1
