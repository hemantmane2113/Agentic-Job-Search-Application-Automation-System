"""
Logging setup for naukri-agent.

Configures a root logger that writes structured, timestamped lines to
both the console and a rotating log file. No caller should configure
logging on its own — call setup_logging() once, at process start
(CLI entrypoint / scheduler start), then use logging.getLogger(__name__)
everywhere else as usual.

IMPORTANT: never pass secrets (passwords, API keys, session cookies)
into a log message. This module does not scrub log content — callers
are responsible for keeping credentials out of what they log.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from naukri_agent.config import Settings

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(settings: Settings) -> None:
    """
    Configure the root logger according to `settings`. Safe to call
    more than once (e.g. in tests) — existing handlers are cleared
    first so log lines are never duplicated.
    """
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_file = Path(settings.log_dir) / "naukri_agent.log"

    root_logger = logging.getLogger()
    root_logger.setLevel(settings.log_level)

    # Clear any handlers from a previous setup_logging() call.
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,  # 5 MB per file
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)
