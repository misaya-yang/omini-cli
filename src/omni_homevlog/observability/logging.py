"""Structured logging.

One JSON object per line so job runs are greppable and machine-readable. Every
record goes through `redact()` on the way out — there is no un-redacted path.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

from omni_homevlog.observability.redaction import redact

LOGGER_NAME = "omni_homevlog"
_CONFIGURED = False
_ACTIVE_FORMAT: str | None = None


class RedactingJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), ensure_ascii=False)


class HumanFormatter(logging.Formatter):
    """Readable local output; still redacted."""

    def format(self, record: logging.LogRecord) -> str:
        base = f"{record.levelname:<7} {record.getMessage()}"
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict) and extra:
            base += "  " + json.dumps(redact(extra), ensure_ascii=False)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


def configure_logging(level: str | None = None, *, json_output: bool | None = None) -> None:
    """Idempotent logging setup."""
    global _CONFIGURED

    resolved_level = (level or os.environ.get("OMNI_LOG_LEVEL", "INFO")).upper()
    if json_output is None:
        json_output = os.environ.get("OMNI_LOG_JSON", "").lower() in {"1", "true", "yes"}

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, resolved_level, logging.INFO))
    logger.propagate = False

    # Re-install whenever the requested format changes. Returning early on
    # `_CONFIGURED` meant the first call won forever — and the CLI configures at
    # import time, so `--log-json` was accepted, ignored, and never reported.
    global _ACTIVE_FORMAT
    wanted = "json" if json_output else "human"
    if _CONFIGURED and wanted == _ACTIVE_FORMAT:
        return

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(RedactingJsonFormatter() if json_output else HumanFormatter())
    logger.handlers = [handler]
    _CONFIGURED = True
    _ACTIVE_FORMAT = wanted

    # google-auth is chatty on refresh; keep it out of the user's face.
    for noisy in ("google.auth", "urllib3", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> logging.Logger:
    configure_logging()
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def log_event(logger: logging.Logger, message: str, /, **fields: Any) -> None:
    """Emit a structured event with arbitrary fields."""
    logger.info(message, extra={"extra_fields": fields})


def write_debug_fixture(path: str, payload: Any) -> str:
    """Persist a redacted raw provider response next to the job.

    §26 requires preserving raw responses in *redacted* debug fixtures. Returns
    the path written.
    """
    import pathlib

    # The shared atomic writer: unique temp name, flushed and fsynced before the
    # replace. The hand-rolled version used a fixed `.tmp` suffix, so two writers
    # for the same fixture collided, and it skipped the flush.
    from omni_homevlog.storage.local import atomic_write_json

    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(target, redact(payload))
    return str(target)
