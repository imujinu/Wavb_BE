from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DomainType = Literal["general", "legal", "medical", "science", "it", "religion"]
TranscriptStatus = Literal["uploaded", "processing", "completed", "failed"]


class TranscriptCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    domain_type: str
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

    domain_type: str
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
    text_morphemes: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding_model: str | None = None
    embedding: list[float] | None = None


# RAG 검색 결과 단일 히트를 나타내는 읽기 전용 모델.
# keyword 점수와 vector 점수를 가중 합산한 최종 score를 포함한다.
class SearchChunkHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    transcript_id: UUID
    parent_chunk_id: UUID
    child_index: int
    start_seconds: float | None
    end_seconds: float | None
    text: str
    # 가중 합산 점수: 0.6 * keyword_score + 0.4 * vector_score
    score: float
    embedding_model: str | None = None


# RAG 검색 시 상위 청크(parent chunk)의 전체 문맥을 반환하는 모델.
# 검색 히트된 child chunk의 parent를 조회하여 풍부한 메타데이터와 함께 응답한다.
class ParentChunkResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    transcript_id: UUID
    domain_type: str
    chunk_index: int
    topic: str | None
    subtopic: str | None
    keywords: list[str]
    speaker_labels: list[str]
    start_seconds: float | None
    end_seconds: float | None
    text: str
    summary: str | None
    metadata: dict[str, Any]


# transcripts 테이블에서 id로 단건 조회한 읽기 전용 모델.
# 요약 PDF 생성의 입력(full_text + 메타)을 담으며, get_transcript_by_id()가 반환한다.
class TranscriptDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID | None
    domain_type: str
    title: str | None
    full_text: str | None
    summary: str | None
    duration_seconds: float | None
    language: str | None
    status: str
    created_at: Any | None = None


# summary_documents 테이블 insert용 모델.
# 생성된 구조화 요약 payload와 어떤 transcript/template로 만들었는지 메타를 함께 담는다.
class SummaryDocumentCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    transcript_id: UUID
    user_id: UUID | None = None
    template_id: str = Field(min_length=1)
    payload: dict[str, Any]
    model: str | None = None


# summary_documents 테이블에서 읽어온 읽기 전용 모델.
# 수정→재렌더 경로에서 저장된 template_id/payload를 다시 꺼내기 위해 사용한다.
class SummaryDocumentDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    transcript_id: UUID
    user_id: UUID | None
    template_id: str
    payload: dict[str, Any]
    model: str | None


# POST /rag/query 엔드포인트 요청 모델.
# query는 자연어 검색 질의, transcript_id는 특정 트랜스크립트로 검색 범위 한정 시 사용.
class RagQueryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1)
    transcript_id: UUID | None = None
    # JWT 도입 전 임시 필드 — 추후 액세스 토큰에서 추출하는 방식으로 대체 예정
    user_id: UUID | None = None
    top_k: int = Field(default=5, ge=1, le=20)


# POST /rag/query 엔드포인트 응답 모델.
# LLM이 생성한 answer와 근거가 된 parent chunk 목록을 함께 반환한다.
class RagQueryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    answer: str
    sources: list[ParentChunkResult]
    chunks_retrieved: int
