import os
import sys
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from app.utils.logger import logger

router = APIRouter(prefix="/api")

class BrowseRequest(BaseModel):
    path: Optional[str] = None

class DirectoryItem(BaseModel):
    name: str
    absolutePath: str

class BrowseResponse(BaseModel):
    currentDir: str
    parentDir: Optional[str] = None
    subdirs: List[DirectoryItem]

def get_home_dir() -> Path:
    return Path.home()

@router.post("/browse", response_model=BrowseResponse)
async def browse_directory(req: BrowseRequest):
    """
    Lists subdirectories of the requested path.
    Defaults to the user's home directory if no path is provided.
    """
    path_str = req.path
    if not path_str or path_str.strip() == "":
        target_path = get_home_dir()
    else:
        target_path = Path(path_str)

    try:
        # Resolve to absolute path
        target_path = target_path.resolve()
        home = get_home_dir().resolve()
        if not (target_path == home or home in target_path.parents):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access restricted to home directory and its subdirectories."
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to resolve path {path_str}: {e}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid path representation: {e}"
        )

    if not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="The requested path does not exist on the server."
        )

    if not target_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The requested path is a file, not a directory."
        )

    subdirs: List[DirectoryItem] = []
    try:
        # Scan directory contents
        for entry in os.scandir(target_path):
            try:
                if entry.is_dir() and not entry.name.startswith("."):
                    subdirs.append(
                        DirectoryItem(
                            name=entry.name,
                            absolutePath=str(Path(entry.path).resolve())
                        )
                    )
            except OSError:
                # Skip directories we can't access
                continue
    except PermissionError as e:
        logger.error(f"Permission denied reading directory {target_path}: {e}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied accessing this directory."
        )
    except Exception as e:
        logger.error(f"Failed to read directory {target_path}: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to list directory: {e}"
        )

    # Sort directories alphabetically by name
    subdirs.sort(key=lambda x: x.name.lower())

    # Resolve parent directory
    parent_path = target_path.parent
    home = get_home_dir().resolve()
    if parent_path == target_path or not (parent_path == home or home in parent_path.parents):
        parent_dir_str = None
    else:
        parent_dir_str = str(parent_path)

    return BrowseResponse(
        currentDir=str(target_path),
        parentDir=parent_dir_str,
        subdirs=subdirs
    )

async def pick_directory_system(initial_dir: Optional[str] = None) -> Optional[str]:
    """
    Opens a native system directory selection dialog.
    Tries AppleScript on macOS first, falls back to Tkinter.
    Runs in a threadpool to prevent blocking the async loop.
    """
    import asyncio
    import subprocess
    import sys

    def _sync_pick():
        # Try AppleScript on macOS first for native look and focus
        if sys.platform == "darwin":
            try:
                default_path = initial_dir or os.path.expanduser("~")
                script = f'POSIX path of (choose folder with prompt "Select Download Destination" default location POSIX file "{default_path}")'
                res = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=120
                )
                if res.returncode == 0:
                    selected_path = res.stdout.strip()
                    if selected_path:
                        return selected_path
                # If user cancelled or failed, return None
                if "User canceled" in res.stderr or res.returncode == 1:
                    return None
            except Exception as e:
                logger.warning(f"macOS AppleScript dialog failed, falling back to Tkinter: {e}")

        # Fallback to Tkinter (works on macOS/Windows/Linux)
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.focus_force()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                initialdir=initial_dir or os.path.expanduser("~"),
                title="Select Download Destination"
            )
            root.destroy()
            return selected if selected else None
        except Exception as e:
            logger.error(f"Failed to open system directory dialog: {e}")
            return None

    return await asyncio.to_thread(_sync_pick)
