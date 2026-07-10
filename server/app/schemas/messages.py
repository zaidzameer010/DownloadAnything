from typing import Annotated, Any, Dict, List, Literal, Optional, Union
from pydantic import BaseModel, Field
from app.schemas.formats import FormatSummary
from app.api.categories import Category
from app.api.settings import AppSettings
from app.api.browse import DirectoryItem
from app.engine.jobs import JobInfo


# ==========================================
# Client -> Server Messages
# ==========================================

class ClientHelloMessage(BaseModel):
    type: Literal["hello"]
    clientVersion: str
    tabId: int

class ClientProbeMessage(BaseModel):
    type: Literal["probe"]
    url: str
    title: Optional[str] = None

class ClientChooseMessage(BaseModel):
    type: Literal["choose"]
    jobId: str
    formatId: str
    outputDir: str
    outTemplate: Optional[str] = None
    conflictResolution: Optional[str] = "replace"
    
    # Optional metadata fields for direct/intercepted downloads
    url: Optional[str] = None
    title: Optional[str] = None
    referer: Optional[str] = None
    fileSize: Optional[int] = None
    mime: Optional[str] = None

class ClientCheckFileExistsMessage(BaseModel):
    type: Literal["check_file_exists"]
    path: str
    filename: str
    jobId: str

class ClientRevealFileMessage(BaseModel):
    type: Literal["reveal_file"]
    jobId: str

class ClientPingMessage(BaseModel):
    type: Literal["ping"]
    ts: float

class ClientGetJobsMessage(BaseModel):
    type: Literal["get_jobs"]

class ClientGetCategoriesMessage(BaseModel):
    type: Literal["get_categories"]

class ClientSaveCategoriesMessage(BaseModel):
    type: Literal["save_categories"]
    categories: List[Category]

class ClientBrowseDirectoryMessage(BaseModel):
    type: Literal["browse_directory"]
    path: Optional[str] = None
    forField: Optional[str] = None

class ClientGetSettingsMessage(BaseModel):
    type: Literal["get_settings"]

class ClientSaveSettingsMessage(BaseModel):
    type: Literal["save_settings"]
    settings: AppSettings

class ClientPauseMessage(BaseModel):
    type: Literal["pause"]
    jobId: str

class ClientResumeMessage(BaseModel):
    type: Literal["resume"]
    jobId: str

class ClientRemoveJobMessage(BaseModel):
    type: Literal["remove_job"]
    jobId: str

class ClientDeleteFileMessage(BaseModel):
    type: Literal["delete_file"]
    jobId: str

class ClientUnknownMessage(BaseModel):
    type: str
    model_config = {"extra": "allow"}

class ClientCancelProbeMessage(BaseModel):
    type: Literal["cancel_probe"]
    jobId: str

# Discriminated/fallback union of all messages sent by Client to Server
ClientMessage = Annotated[
    Union[
        ClientHelloMessage,
        ClientProbeMessage,
        ClientChooseMessage,
        ClientRevealFileMessage,
        ClientPauseMessage,
        ClientResumeMessage,
        ClientRemoveJobMessage,
        ClientDeleteFileMessage,
        ClientPingMessage,
        ClientGetJobsMessage,
        ClientGetCategoriesMessage,
        ClientSaveCategoriesMessage,
        ClientBrowseDirectoryMessage,
        ClientGetSettingsMessage,
        ClientSaveSettingsMessage,
        ClientCheckFileExistsMessage,
        ClientCancelProbeMessage,
        ClientUnknownMessage
    ],
    Field(union_mode="left_to_right")
]



# ==========================================
# Server -> Client Messages
# ==========================================

class ServerHelloMessage(BaseModel):
    type: Literal["hello"] = "hello"
    serverVersion: str
    ytDlpVersion: str
    ffmpegAvailable: bool
    heartbeatIntervalMs: int = 20000

class ServerProbeStartedMessage(BaseModel):
    type: Literal["probe_started"] = "probe_started"
    jobId: str
    url: str

class ServerProbeResultMessage(BaseModel):
    type: Literal["probe_result"] = "probe_result"
    jobId: str
    title: str
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None
    formats: List[FormatSummary]
    mediaType: Optional[str] = None

class ServerProbeFailedMessage(BaseModel):
    type: Literal["probe_failed"] = "probe_failed"
    jobId: str
    error: str
    suggestion: Optional[str] = None  # e.g., 'cookies_required', 'po_token_required'

class ServerDownloadQueuedMessage(BaseModel):
    type: Literal["download_queued"] = "download_queued"
    jobId: str
    outputPath: str
    url: Optional[str] = None
    title: Optional[str] = None
    duration: Optional[float] = None
    thumbnail: Optional[str] = None
    uploader: Optional[str] = None

class ServerDownloadProgressMessage(BaseModel):
    type: Literal["download_progress"] = "download_progress"
    jobId: str
    status: Literal["downloading", "postprocessing"]
    downloadedBytes: Optional[int] = None
    totalBytes: Optional[int] = None
    totalBytesEstimate: Optional[int] = None
    speed: Optional[float] = None
    eta: Optional[float] = None
    fragmentIndex: Optional[int] = None
    fragmentCount: Optional[int] = None
    filePath: Optional[str] = None

class ServerDownloadCompletedMessage(BaseModel):
    type: Literal["download_completed"] = "download_completed"
    jobId: str
    filePath: str
    sizeBytes: Optional[int] = None
    durationMs: Optional[float] = None

class ServerDownloadFailedMessage(BaseModel):
    type: Literal["download_failed"] = "download_failed"
    jobId: str
    error: str
    stage: str  # e.g., 'downloading', 'writing', 'postprocessing'

class ServerDownloadCanceledMessage(BaseModel):
    type: Literal["download_canceled"] = "download_canceled"
    jobId: str

class ServerPongMessage(BaseModel):
    type: Literal["pong"] = "pong"
    ts: float

class ServerJobsListMessage(BaseModel):
    type: Literal["jobs_list"] = "jobs_list"
    jobs: List[JobInfo]

class ServerCategoriesListMessage(BaseModel):
    type: Literal["categories_list"] = "categories_list"
    categories: List[Category]

class ServerDirectoryContentsMessage(BaseModel):
    type: Literal["directory_contents"] = "directory_contents"
    currentDir: str
    parentDir: Optional[str] = None
    subdirs: List[DirectoryItem]

class ServerSettingsDataMessage(BaseModel):
    type: Literal["settings_data"] = "settings_data"
    settings: AppSettings

class ServerBrowseFailedMessage(BaseModel):
    type: Literal["browse_failed"] = "browse_failed"
    error: str

class ServerDirectorySelectedMessage(BaseModel):
    type: Literal["directory_selected"] = "directory_selected"
    path: str
    forField: Optional[str] = None

class ServerDuplicateJobAlertMessage(BaseModel):
    type: Literal["duplicate_job_alert"] = "duplicate_job_alert"
    jobId: str
    url: str
    title: str
    status: str

class ServerFileExistsResultMessage(BaseModel):
    type: Literal["file_exists_result"] = "file_exists_result"
    exists: bool
    filename: str
    path: str
    jobId: str

class ServerUnknownMessage(BaseModel):
    type: str
    model_config = {"extra": "allow"}

ServerMessage = Annotated[
    Union[
        ServerHelloMessage,
        ServerProbeStartedMessage,
        ServerProbeResultMessage,
        ServerProbeFailedMessage,
        ServerDownloadQueuedMessage,
        ServerDownloadProgressMessage,
        ServerDownloadCompletedMessage,
        ServerDownloadFailedMessage,
        ServerDownloadCanceledMessage,
        ServerPongMessage,
        ServerJobsListMessage,
        ServerCategoriesListMessage,
        ServerDirectoryContentsMessage,
        ServerSettingsDataMessage,
        ServerBrowseFailedMessage,
        ServerDirectorySelectedMessage,
        ServerDuplicateJobAlertMessage,
        ServerFileExistsResultMessage,
        ServerUnknownMessage
    ],
    Field(union_mode="left_to_right")
]

