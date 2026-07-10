import logging
import sys
from app.config import settings

def setup_logger():
    log_level_str = settings.LOG_LEVEL.upper()
    log_level = getattr(logging, log_level_str, logging.INFO)
    
    # Configure root logger
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout)
        ]
    )
    
    # Return logger helper
    logger = logging.getLogger("downloader")
    logger.setLevel(log_level)
    return logger

logger = setup_logger()
