from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RealtimeTranscriptEvent(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: Literal["ready", "transcript", "error"]
    chunk_index: int | None = None
    text: str | None = None
    start_seconds: float | None = None
    message: str | None = None


class RealtimeSegmentInput(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_index: int = Field(ge=0)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str = Field(min_length=1)


class RealtimeSaveRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain_type: Literal["meeting", "lecture"]
    title: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    segments: list[RealtimeSegmentInput]


class RealtimeSaveResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    transcript_id: UUID
    segment_count: int
