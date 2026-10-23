"""aria2-next RPC daemon lifecycle and binary discovery.

Discovers the standalone `aria2-next` executable (never `aria2c`), spawns and
supervises a persistent JSON-RPC/WebSocket daemon tied to the backend PID via
`--stop-with-process`, and syncs live settings updates over WebSocket via
`aria2.changeGlobalOption`.
"""

from __future__ import annotations

import os
import platform
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ..settings import DATA_DIR, DEFAULT_UA
from .rpc import Aria2RpcClient, Aria2RpcError

if TYPE_CHECKING:
    from ..settings import AppSettings

log = structlog.get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _ensure_executable(path: Path) -> bool:
    if not path.is_file():
        return False
    if os.name != "nt" and not os.access(path, os.X_OK):
        try:
            path.chmod(path.stat().st_mode | 0o755)
        except OSError:
            return False
    return os.access(path, os.X_OK)


def _repo_candidates() -> list[Path]:
    """Standalone aria2-next release binaries in the repo root or data dir."""
    sys_name = platform.system().lower()
    machine = platform.machine().lower()
    arch_tags: tuple[str, ...]
    if machine in ("arm64", "aarch64"):
        arch_tags = ("arm64", "aarch64")
    else:
        arch_tags = ("x86_64", "amd64", "x64")

    plat_tag = (
        "macos"
        if sys_name == "darwin"
        else "windows"
        if sys_name == "windows"
        else "linux"
    )
    candidates: list[Path] = []
    for base in (REPO_ROOT, DATA_DIR, DATA_DIR / "aria2-next"):
        if not base.is_dir():
            continue
        exact = base / ("aria2-next.exe" if os.name == "nt" else "aria2-next")
        if exact.is_file():
            candidates.append(exact)
        for p in sorted(base.glob("aria2-next-*"), reverse=True):
            if not p.is_file() or p.suffix in (".sha256", ".sig", ".txt", ".md"):
                continue
            name_low = p.name.lower()
            if plat_tag in name_low and any(a in name_low for a in arch_tags):
                candidates.append(p)
        for p in sorted(base.glob("aria2-next-*"), reverse=True):
            if p.is_file() and p not in candidates and p.suffix not in (".sha256", ".sig", ".txt", ".md"):
                candidates.append(p)
    return candidates


def resolve_aria2_next(s: "AppSettings | None" = None) -> str | None:
    """Resolve the `aria2-next` binary path (never `aria2c`)."""
    custom = getattr(s, "aria2_next_path", "") if s is not None else ""
    if custom:
        expanded = Path(os.path.expanduser(custom))
        if _ensure_executable(expanded):
            return str(expanded)

    if found := shutil.which("aria2-next"):
        return found

    for cand in _repo_candidates():
        if _ensure_executable(cand):
            return str(cand)

    for std_path in (
        "/opt/homebrew/bin/aria2-next",
        "/usr/local/bin/aria2-next",
        "/Applications/Rayburst.app/Contents/MacOS/aria2-next",
    ):
        p = Path(std_path)
        if _ensure_executable(p):
            return str(p)
    return None


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_free_port(preferred: int) -> int:
    if 1024 <= preferred <= 65535 and _port_available(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def global_options_from_settings(s: "AppSettings") -> dict[str, str]:
    """Map AppSettings fields to aria2-next global/task option pairs."""
    alloc = s.aria2_file_allocation if s.aria2_file_allocation in ("none", "prealloc", "falloc", "trunc") else "none"
    tls_ver = s.aria2_min_tls_version if s.aria2_min_tls_version in ("TLSv1.1", "TLSv1.2", "TLSv1.3") else "TLSv1.2"
    mfmt = s.media_format if s.media_format in ("mp4", "mkv", "vtt") else "mp4"
    return {
        # Task legs plus their parallel-segment fetch tasks must not queue
        # behind the per-task download cap — but the product is still
        # bounded: each in-flight gid opens up to `split` sockets, and a few
        # hundred queued TCP connections get SYN-dropped by the remote/NAT
        # and stall the whole pool (measured: ~450 sockets in SYN_SENT).
        "max-concurrent-downloads": str(
            max(8, min(48, max(1, s.max_concurrent_downloads) * (s.parallel_segments or 16)))
        ),
        "split": str(max(1, min(256, s.aria2_split or s.parallel_segments or 16))),
        "max-connection-per-server": str(
            max(1, min(256, s.aria2_max_connection_per_server or s.parallel_segments or 16))
        ),
        "min-split-size": s.aria2_min_split_size or "1M",
        "file-allocation": alloc,
        "timeout": str(max(1, min(600, s.timeout_secs or 30))),
        # A connect that hasn't answered in ~15s is a dropped SYN, not a
        # slow server — fail it fast so the slot retries on a fresh socket
        # instead of sitting in SYN_SENT for the OS's ~75s timeout.
        "connect-timeout": str(max(1, min(15, s.timeout_secs or 30))),
        "max-tries": str(max(0, s.retries if s.retries is not None else 3)),
        "retry-wait": str(max(0, min(600, s.aria2_retry_wait or 0))),
        "lowest-speed-limit": s.aria2_lowest_speed_limit or "0",
        "max-overall-download-limit": s.aria2_max_overall_download_limit or "0",
        "max-download-limit": s.aria2_max_download_limit or "0",
        "check-certificate": "true" if s.check_certificate else "false",
        "min-tls-version": tls_ver,
        "user-agent": DEFAULT_UA,
        "media-format": mfmt,
        "state-save-interval": str(max(1, min(600, s.aria2_state_save_interval or 10))),
        # BitTorrent peer port — Global scope; changeGlobalOption applies it
        # to the libtorrent session live.
        "listen-port": str(max(1, min(65535, s.bt_listen_port or 6881))),
    }


class Aria2NextDaemon:
    """Supervises the persistent aria2-next RPC daemon and its WebSocket client."""

    def __init__(self, state_root: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._state_root = state_root or (DATA_DIR / "aria2-next")
        self._secret = secrets.token_hex(16)
        self._port = 16802
        self._binary_path: str | None = None
        self._proc: subprocess.Popen[bytes] | None = None
        self.rpc = Aria2RpcClient(port=self._port, secret=self._secret)
        self._version_info: dict[str, Any] = {}

    def is_running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def ensure_running(self, s: "AppSettings") -> Aria2RpcClient:
        """Start (or reuse) the aria2-next RPC daemon and return its WS client."""
        with self._lock:
            exe = resolve_aria2_next(s)
            if not exe:
                raise RuntimeError("aria2-next binary not found")

            wanted_port = max(1024, min(65535, s.aria2_rpc_port or 16802))
            if (
                self._proc is not None
                and self._proc.poll() is None
                and self._binary_path == exe
            ):
                try:
                    ver = self.rpc.get_version(timeout=2.0)
                    # `product` is absent only on builds predating the field;
                    # anything else naming another product is not ours.
                    if ver.get("product") in (None, "", "aria2-next"):
                        self._version_info = ver
                        return self.rpc
                except Exception:
                    log.warning("aria2-next RPC unresponsive — restarting daemon")
                    self._stop_locked()

            self._stop_locked()
            self._state_root.mkdir(parents=True, exist_ok=True)
            session_file = self._state_root / "aria2.session"
            if not session_file.exists():
                session_file.touch()
            log_file = self._state_root / "aria2-next.log"

            port = _pick_free_port(wanted_port)
            self._port = port
            self._binary_path = exe
            self.rpc.configure(port=port, secret=self._secret)

            gopts = global_options_from_settings(s)
            cmd = [
                exe,
                "--no-conf=true",
                "--enable-rpc=true",
                f"--rpc-listen-port={port}",
                "--rpc-listen-all=false",
                f"--rpc-secret={self._secret}",
                "--rpc-max-request-size=16M",
                f"--state-dir={self._state_root}",
                f"--save-session={session_file}",
                f"--log={log_file}",
                "--log-level=warn",
                "--quiet=true",
                "--console-log-level=warn",
                "--summary-interval=0",
                "--download-result=hide",
                f"--stop-with-process={os.getpid()}",
                *(f"--{k}={v}" for k, v in gopts.items()),
            ]

            log.info("spawning aria2-next RPC daemon", binary=exe, port=port)
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            deadline = time.monotonic() + 6.0
            last_err: Exception | None = None
            while time.monotonic() < deadline:
                if self._proc.poll() is not None:
                    stderr_out = ""
                    if log_file.is_file():
                        try:
                            stderr_out = log_file.read_text("utf-8", "ignore").strip()[-500:]
                        except Exception:
                            pass
                    raise RuntimeError(
                        f"aria2-next exited immediately (code {self._proc.returncode})"
                        + (f": {stderr_out}" if stderr_out else "")
                    )
                try:
                    ver = self.rpc.get_version(timeout=1.0)
                    if ver.get("product") and ver.get("product") != "aria2-next":
                        self._stop_locked()
                        raise RuntimeError(
                            f"Refusing non-aria2-next binary (product={ver.get('product')!r})"
                        )
                    self._version_info = ver
                    log.info(
                        "aria2-next RPC daemon ready",
                        version=ver.get("version"),
                        port=port,
                        media_features=ver.get("mediaFeatures"),
                    )
                    return self.rpc
                except (ConnectionError, OSError, TimeoutError, Aria2RpcError) as e:
                    last_err = e
                    time.sleep(0.05)

            self._stop_locked()
            raise RuntimeError(f"aria2-next RPC failed to start on port {port}: {last_err}")

    def sync_settings(self, s: "AppSettings") -> None:
        """Apply updated AppSettings to the running aria2-next daemon over WS RPC."""
        with self._lock:
            if s.downloader_engine != "aria2_next":
                # Engine switched away — free the port and the process rather
                # than leaving an idle daemon behind.
                if self._proc is not None and self._proc.poll() is None:
                    log.info("aria2-next engine deselected — stopping daemon")
                self._stop_locked()
                return
            exe = resolve_aria2_next(s)
            if not exe:
                return
            wanted_port = max(1024, min(65535, s.aria2_rpc_port or 16802))
            if (
                self._proc is None
                or self._proc.poll() is not None
                or self._binary_path != exe
                or (self._port != wanted_port and _port_available(wanted_port))
            ):
                try:
                    self.ensure_running(s)
                except Exception as e:
                    log.warning("aria2-next daemon start on settings sync failed", error=str(e))
                return

            try:
                self.rpc.change_global_option(global_options_from_settings(s))
            except Exception as e:
                log.warning("aria2-next changeGlobalOption failed — restarting", error=str(e))
                try:
                    self.ensure_running(s)
                except Exception as restart_err:
                    log.warning("aria2-next restart failed", error=str(restart_err))

    def status_dict(self, s: "AppSettings") -> dict[str, Any]:
        """Status snapshot for UI and engine_status / ytdlp_info responses."""
        exe = resolve_aria2_next(s)
        running = self.is_running()
        ver = self._version_info.get("version") if running else None
        media_features = self._version_info.get("mediaFeatures") if running else []
        enabled_features = self._version_info.get("enabledFeatures") if running else []
        if exe and not ver:
            try:
                out = subprocess.run(
                    [exe, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=2.0,
                    check=False,
                ).stdout
                first_line = (out.splitlines() or [""])[0].strip()
                if first_line:
                    ver = first_line.removeprefix("aria2-next version ").removeprefix(
                        "aria2 version "
                    ).strip()
            except Exception:
                pass
        return {
            "available": exe is not None,
            "running": running,
            "binary_path": exe,
            "version": ver,
            "rpc_port": self._port if running else (s.aria2_rpc_port or 16802),
            "enabled_features": enabled_features or [],
            "media_features": media_features or [],
        }

    def _stop_locked(self) -> None:
        self.rpc.disconnect()
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

    def shutdown(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                try:
                    self.rpc.shutdown(force=True)
                except Exception:
                    pass
            self._stop_locked()


_DEFAULT_DAEMON: Aria2NextDaemon | None = None
_DAEMON_LOCK = threading.Lock()


def get_daemon() -> Aria2NextDaemon:
    global _DEFAULT_DAEMON
    with _DAEMON_LOCK:
        if _DEFAULT_DAEMON is None:
            _DEFAULT_DAEMON = Aria2NextDaemon()
        return _DEFAULT_DAEMON
