"""Logging + URL redaction (ports src/log.rs essentials).

structlog end to end: the console renders colored, syntax-highlighted
`key=value` fields (structlog.dev.ConsoleRenderer) at INFO (DOWNLOADANYTHING_LOG
or DOWNLOADANYTHING_DEBUG overrides); the file mirror next to downloadanything.db
writes the same rendered lines, at the same level, without colors, and rotates
past 5 MB via RotatingFileHandler. stdlib-originated records (uvicorn, httpx,
asyncio) flow through the same pipeline via foreign_pre_chain, and yt-dlp's own
screen output joins via YdlLogger.
"""

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

LOG_MAX_BYTES = 5_000_000


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean_text(s: str) -> str:
    """Free text for logging/display — ANSI escapes stripped."""
    return _ANSI_RE.sub("", s)


_SHARED_PRE: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=False),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]

# stdlib records (third-party libs, uvicorn) enter the chain mid-way.
_FOREIGN_PRE: list[Any] = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.ExtraAdder(),
    structlog.processors.TimeStamper(fmt="iso", utc=False),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def is_debug_enabled() -> bool:
    env_log = os.environ.get("DOWNLOADANYTHING_LOG", "").upper()
    env_debug = os.environ.get("DOWNLOADANYTHING_DEBUG", "").lower()
    return env_log == "DEBUG" or env_debug in ("1", "true", "yes")


class _YdlLogger:
    """yt-dlp's `logger` hook — its screen output joins the structlog pipeline.

    yt-dlp reports everything it prints through `to_screen` as debug, so those
    map to info: the console already showed them at that verbosity.
    """

    _log = structlog.get_logger("downloadanything.ytdlp")

    def debug(self, msg: str) -> None:
        self._log.info(clean_text(str(msg)))

    def warning(self, msg: str) -> None:
        self._log.warning(clean_text(str(msg)))

    def error(self, msg: str) -> None:
        self._log.error(clean_text(str(msg)))


YDL_LOGGER = _YdlLogger()


def _formatter(colors: bool) -> structlog.stdlib.ProcessorFormatter:
    """One formatter shape for every handler — only color differs."""
    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_FOREIGN_PRE,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=colors),
        ],
    )


def init_log(log_path: Path | None) -> None:
    """Console and file render the same lines; only the file drops colors."""
    level = (
        logging.DEBUG
        if is_debug_enabled()
        else getattr(logging, os.environ.get("DOWNLOADANYTHING_LOG", "INFO").upper(), logging.INFO)
    )

    structlog.configure(
        processors=[
            *_SHARED_PRE,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(_formatter(colors=True))
    root.addHandler(console)

    # websockets DEBUG dumps every wire frame — uvicorn's ws protocol logs
    # each frame, so INFO+ here unless an explicit debug level is requested.
    logging.getLogger("websockets").setLevel(level if level <= logging.DEBUG else logging.WARNING)

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = RotatingFileHandler(
                log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=1,
                encoding="utf-8",
            )
            fh.setLevel(level)
            fh.setFormatter(_formatter(colors=False))
            root.addHandler(fh)
        except OSError as e:
            logging.getLogger(__name__).warning("cannot open log file %s: %s", log_path, e)

    if "RUST_LOG" not in os.environ:
        if is_debug_enabled():
            os.environ["RUST_LOG"] = "debug,downloadanything_rs=debug"
        else:
            lvl_name = os.environ.get("DOWNLOADANYTHING_LOG", "info").lower()
            os.environ["RUST_LOG"] = f"{lvl_name},downloadanything_rs={lvl_name}"

    try:
        from . import downloadanything_rs

        downloadanything_rs.init_logging(
            os.environ["RUST_LOG"], str(log_path) if log_path is not None else None
        )
    except Exception as e:
        # The Rust bridge is optional (built by scripts/bundle-backend.ts); a
        # missing or stale extension must not break logging setup.
        logging.getLogger(__name__).debug("downloadanything_rs logging bridge unavailable: %s", e)
