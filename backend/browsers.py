"""Detect installed Chromium browsers and load the extension into them.

"Installing" launches the browser with ``--load-extension`` pointing at the
bundled extension folder. Chromium only honors that flag on a fresh launch,
so if the browser is already running the user must quit it first.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Browser:
    id: str
    name: str
    mac_apps: tuple[str, ...]
    linux_bins: tuple[str, ...]
    win_paths: tuple[str, ...]


_BROWSERS: tuple[Browser, ...] = (
    Browser("chrome", "Google Chrome", ("Google Chrome.app",), ("google-chrome", "google-chrome-stable"), ("Google/Chrome/Application/chrome.exe",)),
    Browser("brave", "Brave", ("Brave Browser.app",), ("brave-browser", "brave"), ("BraveSoftware/Brave-Browser/Application/brave.exe",)),
    Browser("edge", "Microsoft Edge", ("Microsoft Edge.app",), ("microsoft-edge", "microsoft-edge-stable"), ("Microsoft/Edge/Application/msedge.exe",)),
    Browser("chromium", "Chromium", ("Chromium.app",), ("chromium", "chromium-browser"), ("Chromium/Application/chrome.exe",)),
    Browser("vivaldi", "Vivaldi", ("Vivaldi.app",), ("vivaldi", "vivaldi-stable"), ("Vivaldi/Application/vivaldi.exe",)),
    Browser("opera", "Opera", ("Opera.app",), ("opera", "opera-stable"), ("Opera/Application/opera.exe", "Opera/Application/launcher.exe")),
)


def extension_dir() -> Path:
    """Path to the extension source folder (contains manifest.json)."""
    # Bundled apps (Tauri sidecar / PyInstaller) may not have a usable
    # __file__; allow an explicit override and look next to the executable.
    env_path = os.environ.get("DA_EXTENSION_PATH")
    if env_path:
        return Path(env_path)
    roots = []
    if getattr(sys, "frozen", False) or "__compiled__" in globals():
        exe_dir = Path(sys.executable).resolve().parent
        roots.append(exe_dir / "extention")
        # Tauri macOS/Linux Resources layout: Resources/<backend-dir>/<exe>
        # with Resources/extention as a sibling of the backend folder.
        roots.append(exe_dir.parent / "extention")
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass) / "extention")
    try:
        roots.append(Path(__file__).resolve().parent.parent / "extention")
    except NameError:
        pass
    for root in roots:
        if (root / "manifest.json").is_file():
            return root
    return roots[-1] if roots else Path.cwd() / "extention"


def _find_macos(mac_apps: tuple[str, ...]) -> str | None:
    for apps_dir in (Path("/Applications"), Path.home() / "Applications"):
        for app_name in mac_apps:
            macos_dir = apps_dir / app_name / "Contents" / "MacOS"
            if not macos_dir.is_dir():
                continue
            for binary in sorted(macos_dir.iterdir()):
                if binary.is_file() and os.access(binary, os.X_OK):
                    return str(binary)
    return None


def _find_linux(linux_bins: tuple[str, ...]) -> str | None:
    for name in linux_bins:
        path = shutil.which(name)
        if path:
            return path
    return None


def _find_windows(win_paths: tuple[str, ...]) -> str | None:
    roots = [
        Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")),
        Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")),
        Path(os.environ.get("LOCALAPPDATA", "")) if os.environ.get("LOCALAPPDATA") else None,
    ]
    for root in roots:
        if root is None:
            continue
        for relative in win_paths:
            candidate = root / relative
            if candidate.is_file():
                return str(candidate)
    return None


def _find_browser_executable(browser: Browser) -> str | None:
    system = platform.system()
    if system == "Darwin":
        return _find_macos(browser.mac_apps)
    if system == "Linux":
        return _find_linux(browser.linux_bins)
    if system == "Windows":
        return _find_windows(browser.win_paths)
    return None


def _is_browser_running(executable: str) -> bool:
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {Path(executable).name}"],
                capture_output=True, text=True, timeout=10,
            )
            return Path(executable).name.lower() in (result.stdout or "").lower()
        result = subprocess.run(
            ["pgrep", "-f", executable],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


_GRACEFUL_QUIT_TIMEOUT = 15.0
_FORCE_KILL_TIMEOUT = 5.0


def _quit_graceful(executable: str) -> bool:
    """Ask the browser to quit gracefully. Chromium handles SIGTERM/WM_CLOSE
    by shutting down and saving the session for restore."""
    system = platform.system()
    try:
        if system == "Darwin":
            # Chromium apps answer AppleScript by their *internal* name, not
            # the bundle name (e.g. Brave responds to "Brave", not
            # "Brave Browser") — try both, then fall back to SIGTERM, which
            # Chromium handles as a graceful shutdown.
            app_name = Path(executable).parents[1].stem
            exe_stem = Path(executable).stem
            for name in dict.fromkeys((app_name, exe_stem)):
                result = subprocess.run(
                    ["osascript", "-e", f'quit app "{name}"'],
                    capture_output=True, timeout=10,
                )
                if result.returncode == 0:
                    return True
            subprocess.run(
                ["pkill", "-TERM", "-f", executable],
                capture_output=True, timeout=10,
            )
            return True
        if system == "Linux":
            subprocess.run(
                ["pkill", "-TERM", "-f", executable],
                capture_output=True, timeout=10,
            )
            return True
        if system == "Windows":
            # Without /F this sends WM_CLOSE — graceful for windowed processes.
            result = subprocess.run(
                ["taskkill", "/IM", Path(executable).name],
                capture_output=True, timeout=10,
            )
            return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False
    return False


def _force_kill(executable: str) -> None:
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", Path(executable).name],
                capture_output=True, timeout=10,
            )
        else:
            subprocess.run(
                ["pkill", "-9", "-f", executable],
                capture_output=True, timeout=10,
            )
    except (OSError, subprocess.SubprocessError):
        pass


def _wait_for_exit(executable: str, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _is_browser_running(executable):
            return True
        time.sleep(0.25)
    return False


def _quit_and_wait(executable: str) -> str:
    """Quit a running browser. Returns 'quit' (graceful), 'killed' (forced), or 'failed'."""
    if not _is_browser_running(executable):
        return "quit"
    graceful = _quit_graceful(executable)
    if graceful and _wait_for_exit(executable, _GRACEFUL_QUIT_TIMEOUT):
        return "quit"
    _force_kill(executable)
    if _wait_for_exit(executable, _FORCE_KILL_TIMEOUT):
        return "killed"
    return "failed"


def list_browsers() -> list[dict[str, str]]:
    """Return installed Chromium browsers as [{id, name, executable}]."""
    found: list[dict[str, str]] = []
    for browser in _BROWSERS:
        executable = _find_browser_executable(browser)
        if executable:
            found.append({"id": browser.id, "name": browser.name, "executable": executable})
    return found


def install_extension(browser_id: str) -> dict[str, Any]:
    """Launch the browser with the extension loaded via --load-extension."""
    browsers = {b.id: b for b in _BROWSERS}
    browser = browsers.get(browser_id)
    if browser is None:
        return {"ok": False, "error": f"Unknown browser: {browser_id}"}
    installed = _find_browser_executable(browser)
    if not installed:
        return {"ok": False, "error": f"{browser.name} is not installed on this system"}

    ext_path = extension_dir()
    if not (ext_path / "manifest.json").is_file():
        return {"ok": False, "error": f"Extension not found at {ext_path}"}

    # Chromium ignores --load-extension when an instance is already running
    quit_status = "not-running"
    if _is_browser_running(installed):
        quit_status = _quit_and_wait(installed)
        if quit_status == "failed":
            return {
                "ok": False,
                "error": f"{browser.name} did not quit in time. Close it fully and retry.",
            }

    launch_args = [installed, f"--load-extension={ext_path}"]
    if quit_status == "killed":
        # A force kill skips the clean-shutdown marker, so Chromium would show
        # the "Restore pages?" bubble; suppress it and restore the last session.
        launch_args.extend(["--hide-crash-restore-bubble", "--restore-last-session"])

    try:
        popen_kwargs: dict[str, Any] = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        # start_new_session is POSIX-only; passing it on Windows raises.
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        subprocess.Popen(launch_args, **popen_kwargs)
    except OSError as exc:
        return {"ok": False, "error": f"Could not launch {browser.name}: {exc}"}

    if quit_status == "killed":
        message = f"{browser.name} was restarted with the extension loaded (session restore requested)."
    else:
        message = f"Launching {browser.name} with the extension loaded."
    return {"ok": True, "message": message}
