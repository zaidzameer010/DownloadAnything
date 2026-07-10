import os
from pathlib import Path
import tempfile
from typing import List
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
    
    MAX_CONCURRENT_DOWNLOADS: int = 2
    FFMPEG_PATH: str = "ffmpeg"  # Will rely on PATH unless overridden
    LOG_LEVEL: str = "INFO"
    
    # List of allowed origins for CORS. 
    # Since chrome extensions have dynamic IDs, we will handle origin checking dynamically or allow '*'
    ALLOWED_ORIGINS: List[str] = [
        "chrome-extension://*",
        "moz-extension://*",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8765"
    ]
    
    # Directory to store temp Netscape cookies
    COOKIE_JAR_DIR: str = os.path.join(tempfile.gettempdir(), "md_cookies")

settings = Settings()

# Ensure Cookie Jar directory exists
os.makedirs(settings.COOKIE_JAR_DIR, exist_ok=True)

def get_app_data_dir() -> Path:
    import sys
    platform = sys.platform
    if platform == "darwin":
        base_dir = Path.home() / "Library" / "Application Support"
    elif platform == "win32":
        base_dir = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming")))
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

def get_app_version() -> str:
    # 1. Try environment variable passed from Rust/Tauri wrapper
    version_env = os.environ.get("DOWNLOADER_VERSION")
    if version_env:
        return version_env

    # 2. Try reading package.json (dev mode fallback)
    try:
        package_json_path = Path(__file__).resolve().parent.parent.parent / "package.json"
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

