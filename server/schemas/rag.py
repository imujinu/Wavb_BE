from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DomainType = Literal["meeting", "lecture"]
TranscriptStatus = Literal["uploaded", "processing", "completed", "failed"]


class TranscriptCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain_type: DomainType
    source_audio_uri: str = Field(min_length=1)
    user_id: UUID | None = None
    title: str | None = None
    original_filename: str | None = None
    mime_type: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    language: str = Field(default="ko", min_length=1)
    stt_model: str | None = None
    status: TranscriptStatus = "uploaded"


class TranscriptResultUpdate(BaseModel):
    model_config = ConfigDict(frozen=True)

    full_text: str | None = None
    summary: str | None = None
    duration_seconds: float | None = Field(default=None, ge=0)
    stt_model: str | None = None
    status: TranscriptStatus = "completed"
    error_message: str | None = None


class SegmentCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    segment_index: int = Field(ge=0)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str = Field(min_length=1)
    speaker_label: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    raw_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_range(self) -> "SegmentCreate":
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must be greater than or equal to start_seconds")
        return self


class ChunkCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain_type: DomainType
    chunk_index: int = Field(ge=0)
    chunk_strategy: str = Field(min_length=1)
    text: str = Field(min_length=1)
    segment_start_index: int | None = Field(default=None, ge=0)
    segment_end_index: int | None = Field(default=None, ge=0)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    summary: str | None = None
    topic: str | None = None
    subtopic: str | None = None
    keywords: list[str] = Field(default_factory=list)
    speaker_labels: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding_model: str | None = None
    embedding: list[float] | None = None

    @field_validator("keywords", "speaker_labels")
    @classmethod
    def remove_blank_values(cls, values: list[str]) -> list[str]:
        return [value.strip() for value in values if value.strip()]

    @model_validator(mode="after")
    def validate_ranges(self) -> "ChunkCreate":
        if (
            self.segment_start_index is not None
            and self.segment_end_index is not None
            and self.segment_end_index < self.segment_start_index
        ):
            raise ValueError(
                "segment_end_index must be greater than or equal to segment_start_index"
            )
        if (
            self.start_seconds is not None
            and self.end_seconds is not None
            and self.end_seconds < self.start_seconds
        ):
            raise ValueError("end_seconds must be greater than or equal to start_seconds")
        return self


# chunks 테이블에서 읽어온 chunk 행을 나타내는 읽기 전용 모델.
# insert_chunks() 완료 후 DB에서 조회해 parent_chunk_id를 얻기 위해 사용한다.
class ChunkRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    chunk_index: int
    segment_start_index: int | None
    segment_end_index: int | None
    start_seconds: float | None
    end_seconds: float | None
    text: str
    metadata: dict[str, Any]


# search_chunks 테이블 insert용 모델.
# parent chunk(ChunkRow)를 adaptive grouping으로 분할한 child search unit을 표현한다.
class SearchChunkCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    parent_chunk_id: UUID
    child_index: int = Field(ge=0)
    segment_start_index: int = Field(ge=0)
    segment_end_index: int = Field(ge=0)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)
    text: str = Field(min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding_model: str | None = None
    embedding: list[float] | None = None
