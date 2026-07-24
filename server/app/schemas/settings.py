from typing import Optional

from pydantic import BaseModel, field_validator


class AppSettings(BaseModel):
    mergeFormat: str = "mkv"
    embedThumbnail: bool = True
    embedSubs: bool = False
    cookiesFromBrowser: Optional[str] = (
        None  # e.g. "chrome", "firefox", "safari", "none" or null
    )

    @field_validator("mergeFormat", mode="before")
    @classmethod
    def _validate_merge_format(cls, v):
        if not v or not isinstance(v, str):
            return "mkv"
        return v.strip().lstrip(".").lower() or "mkv"

    @field_validator("ffmpegLocation", mode="before")
    @classmethod
    def _validate_ffmpeg_location(cls, v):
        if not v:
            return None
        v_str = str(v).strip()
        if not v_str:
            return None
        name = v_str.replace("\\", "/").split("/")[-1]
        if name.lower() not in ("ffmpeg", "ffmpeg.exe"):
            raise ValueError(
                "ffmpegLocation must point strictly to an executable named 'ffmpeg' or 'ffmpeg.exe'"
            )
        return v_str

    # yt-dlp configs
    concurrentFragmentDownloads: int = 4
    downloadRetries: int = 10
    fragmentRetries: int = 10
    rateLimit: Optional[str] = None  # e.g. "50K", "1M", "5M" or null (unlimited)
    subtitlesLangs: str = "all"  # comma separated list of language tags or "all"
    ffmpegLocation: Optional[str] = None  # custom path to ffmpeg

    # aria2-next configs
    useAria2Next: bool = True
    aria2NextMaxConnections: int = 16
    aria2NextConcurrentDownloads: int = 5
    aria2NextSplit: int = 16
    aria2NextMinSplitSize: str = "1M"
    aria2NextPreallocate: bool = True
    aria2NextCheckCertificate: bool = True
    aria2NextAlwaysResume: bool = True

    # libtorrent configs. Rate limits are KiB/s; zero means unlimited.
    torrentEnabled: bool = True
    torrentMaxActive: int = 4
    torrentDownloadLimit: int = 0
    torrentUploadLimit: int = 0
    torrentSeedRatio: float = 2.0
    torrentPeerLimit: int = 500
    torrentUploadPeerLimit: int = 20

    @field_validator(
        "concurrentFragmentDownloads",
        "aria2NextMaxConnections",
        "aria2NextConcurrentDownloads",
        "aria2NextSplit",
        "torrentMaxActive",
        "torrentPeerLimit",
        "torrentUploadPeerLimit",
        mode="before",
    )
    @classmethod
    def _validate_positive_int(cls, v):
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError("must be an integer")
        if v <= 0:
            raise ValueError("must be a positive integer")
        return v

    @field_validator(
        "downloadRetries",
        "fragmentRetries",
        "torrentDownloadLimit",
        "torrentUploadLimit",
        mode="before",
    )
    @classmethod
    def _validate_non_negative_int(cls, v):
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise ValueError("must be an integer")
        if v < 0:
            raise ValueError("must be a non-negative integer")
        return v

    @field_validator("torrentSeedRatio", mode="before")
    @classmethod
    def _validate_non_negative_float(cls, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            raise ValueError("must be a number")
        if v < 0:
            raise ValueError("must be a non-negative number")
        return v
