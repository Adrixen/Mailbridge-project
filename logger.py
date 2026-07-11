"""
MailBridge — logging setup.

Provides structured logging with rotating file handler and console/stdout output
for systemd journal capture. A per-account logger adapter injects the account
name into every log record.
"""

import logging
import logging.handlers
import os
from typing import Optional


class AccountAdapter(logging.LoggerAdapter):
    """Logger adapter that injects an `account` field into log records."""

    def __init__(self, logger: logging.Logger, account_id: str):
        super().__init__(logger, {"account": account_id})

    def process(self, msg, kwargs):
        kwargs["extra"] = kwargs.get("extra", {})
        kwargs["extra"]["account"] = self.extra.get("account", "?")
        return msg, kwargs


class DefaultAccountFilter(logging.Filter):
    """Ensures every log record has an `account` field, defaulting to `-`."""

    def filter(self, record):
        if not hasattr(record, "account"):
            record.account = "-"
        return True


_FORMAT = "%(asctime)s %(levelname)s [%(name)s/%(account)s] %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(
    log_dir: str = "logs",
    level: int = logging.INFO,
    to_console: bool = True,
) -> logging.Logger:
    """
    Configure root logger with rotating file + optional console handler.

    :param log_dir: directory for log files (created if absent)
    :param level: logging level (e.g. logging.DEBUG)
    :param to_console: if True, also log to stdout (for journalctl capture)
    :return: root logger
    """
    os.makedirs(log_dir, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    default_filter = DefaultAccountFilter()

    # File handler with rotation (5 MB × 5 backups)
    log_path = os.path.join(log_dir, "mailbridge.log")
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
    fh.addFilter(default_filter)
    root.addHandler(fh)

    if to_console:
        ch = logging.StreamHandler()
        ch.setLevel(level)
        ch.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATE_FORMAT))
        ch.addFilter(default_filter)
        root.addHandler(ch)

    return root


def get_account_logger(account_id: str) -> AccountAdapter:
    """Return a logger adapter that tags every message with the account ID."""
    return AccountAdapter(logging.getLogger("worker"), account_id)
