import json
import os
import sys
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DOWNLOADER_",
        case_sensitive=True,
    )

    HOST: str = "127.0.0.1"
    PORT: int = 8765

    # Defaults to the user's Downloads directory
    DEFAULT_OUTPUT_DIR: str = str(Path.home() / "Downloads")

    LOG_LEVEL: str = "INFO"


settings = Settings()

def get_app_data_dir() -> Path:
    platform = sys.platform
    if platform == "darwin":
        base_dir = Path.home() / "Library" / "Application Support"
    elif platform == "win32":
        base_dir = Path(
            os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
        )
    else:
        base_dir = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))

    app_dir = base_dir / "DownloadAnything"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


def get_config_file_path(filename: str) -> Path:
    new_dir = get_app_data_dir()
    new_path = new_dir / filename

    if not new_path.exists():
        # Check old location (server/)
        old_dir = Path(__file__).resolve().parent.parent
        old_path = old_dir / filename
        if old_path.exists():
            try:
                import shutil

                shutil.copy2(old_path, new_path)
            except Exception:
                pass
    return new_path


def write_json_atomic(path: Path, payload: object) -> None:
    tmp_path = path.with_name(f"{path.name}.tmp")
    with open(tmp_path, "w") as file:
        json.dump(payload, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    tmp_path.replace(path)


def get_app_version() -> str:
    # 1. Try environment variable passed from Rust/Tauri wrapper
    version_env = os.environ.get("DOWNLOADER_VERSION")
    if version_env:
        return version_env

    # 2. Try reading package.json (dev mode fallback)
    try:
        package_json_path = (
            Path(__file__).resolve().parent.parent.parent / "package.json"
        )
        if package_json_path.exists():
            import json

            with open(package_json_path, "r") as f:
                version = json.load(f).get("version")
                if version:
                    return version
    except Exception:
        pass

    # 3. Fallback if not resolvable
    return "0.0.0"
