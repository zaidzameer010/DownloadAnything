from typing import Annotated, List, Literal, Optional, Union
from pydantic import BaseModel, Field
from app.schemas.category import Category
from app.schemas.settings import AppSettings


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
    referer: Optional[str] = None
    jobId: Optional[str] = None


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
    filename: Optional[str] = None
    referer: Optional[str] = None
    pageUrl: Optional[str] = None
    fileSize: Optional[int] = None
    mime: Optional[str] = None

    # Optional torrent file selection (indices of files to download)
    torrentSelectedFileIndices: Optional[List[int]] = None


class ClientCheckFileExistsMessage(BaseModel):
    type: Literal["check_file_exists"]
    path: str
    filename: Optional[str] = None
    jobId: str
    # Optional metadata for server-side filename resolution.
    title: Optional[str] = None
    ext: Optional[str] = None
    url: Optional[str] = None
    mime: Optional[str] = None


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


class ClientRefreshUrlMessage(BaseModel):
    type: Literal["refresh_url"]
    jobId: str
    url: str
    referer: Optional[str] = None


class ClientDownloadUrlMessage(BaseModel):
    type: Literal["download_url"]
    jobId: str
    url: str
    referer: Optional[str] = None


class ClientRemoveJobMessage(BaseModel):
    type: Literal["remove_job"]
    jobId: str


class ClientDeleteFileMessage(BaseModel):
    type: Literal["delete_file"]
    jobId: str


class ClientCancelDownloadMessage(BaseModel):
    type: Literal["cancel"]
    jobId: str


class ClientCancelProbeMessage(BaseModel):
    type: Literal["cancel_probe"]
    jobId: str


# Discriminated union of all messages sent by Client to Server. Unknown message
# types are intentionally rejected so version skew and typos do not silently
# turn into no-ops.
ClientMessage = Annotated[
    Union[
        ClientHelloMessage,
        ClientProbeMessage,
        ClientChooseMessage,
        ClientRevealFileMessage,
        ClientPauseMessage,
        ClientResumeMessage,
        ClientRefreshUrlMessage,
        ClientDownloadUrlMessage,
        ClientRemoveJobMessage,
        ClientDeleteFileMessage,
        ClientCancelDownloadMessage,
        ClientPingMessage,
        ClientGetJobsMessage,
        ClientGetCategoriesMessage,
        ClientSaveCategoriesMessage,
        ClientBrowseDirectoryMessage,
        ClientGetSettingsMessage,
        ClientSaveSettingsMessage,
        ClientCheckFileExistsMessage,
        ClientCancelProbeMessage,
    ],
    Field(union_mode="left_to_right"),
]


