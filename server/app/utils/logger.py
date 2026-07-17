import logging
import sys
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def setup_logger():
    log_level_str = settings.LOG_LEVEL.upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Return logger helper
    logger = logging.getLogger("downloader")
    logger.setLevel(log_level)
    return logger


logger = setup_logger()
