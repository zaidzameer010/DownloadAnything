import logging
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import structlog
from structlog.typing import Processor

from app.config import settings


_SENSITIVE_QUERY_KEYS = {
    "access_token",
    "api_key",
    "auth",
    "code",
    "key",
    "sig",
    "signature",
    "token",
}


def redact_url(url: str) -> str:
    """Return a URL with sensitive query values replaced by [REDACTED]."""
    try:
        parsed = urlsplit(url)
        query = [
            (key, "[REDACTED]" if key.lower() in _SENSITIVE_QUERY_KEYS else value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        ]
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
        )
    except ValueError:
        return "[invalid-url]"


def _level_from_name(level: str) -> int:
    """Resolve a level name to a logging level integer."""
    level_upper = level.upper()
    if hasattr(logging, level_upper):
        return getattr(logging, level_upper)
    logging.getLogger(__name__).warning(
        f"Invalid log level {level!r}; defaulting to INFO"
    )
    return logging.INFO


def setup_logging(
    level: str | None = None,
    log_format: str | None = None,
) -> None:
    """
    Configure structured logging for the application.

    This wires structlog into the standard library logging tree so that
    third-party libraries and application code share the same output format
    and level control.

    Parameters
    ----------
    level:
        Override ``settings.LOG_LEVEL``. One of DEBUG/INFO/WARNING/ERROR/CRITICAL.
    log_format:
        ``json`` for machine-readable JSON, ``text`` for human-readable colored
        console output, ``auto`` to pick JSON in frozen/PyInstaller builds and
        text otherwise. Falls back to ``settings.LOG_FORMAT``.
    """
    if structlog.is_configured():
        # Wiring is global and idempotent; re-running would duplicate handlers.
        return

    level_name = (level or settings.LOG_LEVEL).upper()
    level_int = _level_from_name(level_name)

    fmt = (log_format or settings.LOG_FORMAT or "auto").lower()
    if fmt == "auto":
        fmt = "json" if getattr(sys, "frozen", False) else "text"

    shared_processors: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    renderer: Processor
    if fmt == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # Format *all* stdlib log records (including uvicorn and yt-dlp) through
    # the same structlog renderer so the output is uniform.
    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root.addHandler(handler)
    root.setLevel(level_int)

    # Application loggers respect the configured level.
    logging.getLogger("app").setLevel(level_int)

    # No third-party library loggers are forced to a higher level; all logs that
    # propagate to the root logger are formatted by structlog.


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """
    Return a structlog BoundLogger.

    The logger is a lazy proxy until the first log call, so it is safe to
    create at module import time before ``setup_logging()`` has been called.
    """
    return structlog.get_logger(name)


# Re-export contextvar helpers so callers can bind per-request / per-job
# context without depending on structlog directly.
bind_contextvars = structlog.contextvars.bind_contextvars
unbind_contextvars = structlog.contextvars.unbind_contextvars
clear_contextvars = structlog.contextvars.clear_contextvars
bound_contextvars = structlog.contextvars.bound_contextvars
