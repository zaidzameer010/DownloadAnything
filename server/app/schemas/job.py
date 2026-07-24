from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class JobInfo(BaseModel):
    model_config = {"arbitrary_types_allowed": True, "frozen": False}

    job_id: str
    url: str
    status: str  # queued, probing, downloading, postprocessing, seeding, completed, failed, canceled, paused
    progress: float = 0.0
    # Video stream bytes
    downloaded_bytes: float = 0.0
    total_bytes: float = 0.0
    # Audio stream bytes (populated when downloading separate video+audio)
    audio_downloaded_bytes: float = 0.0
    audio_total_bytes: float = 0.0
    # Combined bytes across all streams
    combined_downloaded_bytes: float = 0.0
    combined_total_bytes: float = 0.0
    # Which stream phase: 'video', 'audio', or 'single'
    stream_phase: str = "single"
    speed: float = 0.0
    eta: float = 0.0
    format_id: Optional[str] = None
    output_dir: Optional[str] = None
    error: Optional[str] = None
    error_category: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    file_path: Optional[str] = None
    formats: Optional[List[Any]] = None
    fragment_index: Optional[int] = None
    fragment_count: Optional[int] = None
    referer: Optional[str] = None
    page_url: Optional[str] = None
    probe_format_ids: Optional[List[str]] = None
    probe_timestamp: Optional[float] = None
    probe_referer: Optional[str] = None
    media_type: Optional[str] = None
    mime: Optional[str] = None
    filename: Optional[str] = None
    torrent_files: Optional[List[Dict[str, Any]]] = None
    torrent_selected_file_indices: Optional[List[int]] = None
    torrent_info_hash: Optional[str] = None
    torrent_piece_length: Optional[int] = None
    torrent_piece_count: Optional[int] = None
    torrent_peers: int = 0
    torrent_seeds: int = 0
    torrent_availability: float = 0.0
    torrent_completed_pieces: int = 0
    added_at: float = 0.0
