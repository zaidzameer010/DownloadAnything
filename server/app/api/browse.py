import asyncio
import orjson
import os
import sys
from pathlib import Path
from typing import Optional
from app.services.interfaces import IDirectoryPicker
from app.utils.logger import get_logger

logger = get_logger(__name__)


class DirectoryPicker(IDirectoryPicker):
    """Native system directory selection dialog gateway."""

    async def pick(self, initial_dir: Optional[Path] = None) -> Optional[Path]:
        result = await pick_system_directory(str(initial_dir) if initial_dir else None)
        return Path(result) if result else None


async def pick_system_directory(initial_dir: Optional[str] = None) -> Optional[str]:
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
                script_path = orjson.dumps(default_path).decode()
                script = f'POSIX path of (choose folder with prompt "Select Download Destination" default location POSIX file {script_path})'
                res = subprocess.run(
                    ["osascript", "-e", script],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if res.returncode == 0:
                    selected_path = res.stdout.strip()
                    if selected_path:
                        return selected_path
                # If user cancelled or failed, return None
                if "User canceled" in res.stderr or res.returncode == 1:
                    return None
            except Exception as e:
                logger.warning(
                    f"macOS AppleScript dialog failed, falling back to Tkinter: {e}"
                )

        # Fallback to Tkinter (works on macOS/Windows)
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            try:
                root.withdraw()
                root.focus_force()
                root.attributes(topmost=True)
                selected = filedialog.askdirectory(
                    initialdir=initial_dir or os.path.expanduser("~"),
                    title="Select Download Destination",
                )
                return selected if selected else None
            finally:
                root.destroy()
        except Exception as e:
            logger.error(f"Failed to open system directory dialog: {e}")
            return None

    return await asyncio.to_thread(_sync_pick)
