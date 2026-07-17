from typing import Optional
from pydantic import BaseModel


class FormatSummary(BaseModel):
    label: str
    height: int
    fps: int
    codecFamily: str
    ext: str
    tbr: Optional[float] = None
    estSizeBytes: Optional[int] = None
    formatId: str
    isCombined: bool
    hdr: bool
    videoEstSizeBytes: Optional[int] = None
    audioEstSizeBytes: Optional[int] = None
    isStream: bool = False
    streamType: Optional[str] = None
    videoCodec: Optional[str] = None
    audioCodec: Optional[str] = None
    language: Optional[str] = None
    protocol: Optional[str] = None
    dynamicRange: Optional[str] = None
    compatibility: Optional[str] = None
