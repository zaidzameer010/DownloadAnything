"""Logging + URL redaction (ports src/log.rs essentials).

structlog end to end: the console renders colored, syntax-highlighted
`key=value` fields (structlog.dev.ConsoleRenderer) at INFO (DAX_LOG env
or DAX_DEBUG overrides); the file mirror next to dax.db writes JSON lines
at DEBUG and rotates past 5 MB via RotatingFileHandler. stdlib-originated
records (uvicorn, httpx, asyncio) flow through the same pipeline via
foreign_pre_chain.
"""

import logging
from logging.handlers import RotatingFileHandler
import os
import re
from pathlib import Path
from typing import Any

import orjson
import structlog

LOG_MAX_BYTES = 5_000_000


def _json_serializer(d: dict, **kw: Any) -> str:
    # default=str (overrides structlog's raising fallback): bare exception
    # objects in fields (error=e) serialize instead of dropping the record;
    # ProcessorFormatter needs str back, not orjson's bytes.
    kw["default"] = str
    return orjson.dumps(d, **kw).decode()


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def clean_text(s: str) -> str:
    """Free text for logging/display — ANSI escapes stripped."""
    return _ANSI_RE.sub("", s)


_SHARED_PRE: list = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=False),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]

# stdlib records (third-party libs, uvicorn) enter the chain mid-way.
_FOREIGN_PRE: list = [
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.ExtraAdder(),
    structlog.processors.TimeStamper(fmt="iso", utc=False),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def is_debug_enabled() -> bool:
    env_log = os.environ.get("DAX_LOG", "").upper()
    env_debug = os.environ.get("DAX_DEBUG", "").lower()
    return env_log == "DEBUG" or env_debug in ("1", "true", "yes")


def init_log(log_path: Path | None) -> None:
    """Colored console at env-or-info level; JSON-lines file at debug."""
    level = logging.DEBUG if is_debug_enabled() else getattr(
        logging, os.environ.get("DAX_LOG", "INFO").upper(), logging.INFO
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
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_FOREIGN_PRE,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(colors=True),
            ],
        )
    )
    root.addHandler(console)

    # websockets DEBUG dumps every wire frame — uvicorn's ws protocol logs
    # each frame, so INFO+ here unless an explicit debug level is requested.
    logging.getLogger("websockets").setLevel(
        level if level <= logging.DEBUG else logging.WARNING
    )

    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fh = RotatingFileHandler(
                log_path,
                maxBytes=LOG_MAX_BYTES,
                backupCount=1,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                structlog.stdlib.ProcessorFormatter(
                    foreign_pre_chain=_FOREIGN_PRE,
                    processors=[
                        structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                        structlog.processors.JSONRenderer(
                            serializer=_json_serializer
                        ),
                    ],
                )
            )
            root.addHandler(fh)
        except OSError as e:
            print(f"dax: cannot open log file {log_path}: {e}")

    if "RUST_LOG" not in os.environ:
        if is_debug_enabled():
            os.environ["RUST_LOG"] = "debug,dax_rs=debug"
        else:
            lvl_name = os.environ.get("DAX_LOG", "info").lower()
            os.environ["RUST_LOG"] = f"{lvl_name},dax_rs={lvl_name}"

    try:
        from . import dax_rs
        dax_rs.init_logging(os.environ["RUST_LOG"])
    except Exception:
        pass
