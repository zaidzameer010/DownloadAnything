import asyncio
import os
import sys
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel
from app.utils.logger import logger


class DirectoryItem(BaseModel):
    name: str
    absolutePath: str


def get_home_dir() -> Path:
    return Path.home()


def _list_subdirectories(target_path: Path) -> list[DirectoryItem]:
    subdirs: list[DirectoryItem] = []
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
            continue
    subdirs.sort(key=lambda x: x.name.lower())
    return subdirs


async def pick_directory_system(initial_dir: Optional[str] = None) -> Optional[str]:
    """
    Opens a native system directory selection dialog.
    Tries AppleScript on macOS first, falls back to Tkinter.
    Runs in a threadpool to prevent blocking the async loop.
    """
    import subprocess

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
