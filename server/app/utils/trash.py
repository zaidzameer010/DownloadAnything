from pathlib import Path
from send2trash import send2trash
from app.utils.logger import get_logger

logger = get_logger(__name__)


def send_to_trash(file_path: str) -> None:
    path = Path(file_path).resolve()
    target_path = None
    if path.exists():
        target_path = path
    else:
        part_path = Path(str(path) + ".part")
        if part_path.exists():
            target_path = part_path
            
    if not target_path:
        logger.debug(f"send_to_trash: path does not exist: {file_path}")
        return

    logger.info(f"Moving to trash using send2trash: {target_path}")
    try:
        send2trash(str(target_path))
    except Exception as error:
        logger.error(f"send2trash failed to move {target_path} to trash: {error}")
        raise
