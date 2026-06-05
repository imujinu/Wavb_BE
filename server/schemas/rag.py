from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


TranscriptStatus = Literal["uploaded", "processing", "completed", "failed"]
RagSearchScope = Literal["document", "web", "hybrid"]
SourceRangeType = Literal["audio", "pdf", "ppt"]


class TranscriptCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_audio_uri: str = Field(min_length=1)
    user_id: UUID | None = None
    folder_id: UUID | None = None
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
    source_type: SourceRangeType | None = None
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)
    source_slide_start: int | None = Field(default=None, ge=1)
    source_slide_end: int | None = Field(default=None, ge=1)
    source_start_seconds: float | None = Field(default=None, ge=0)
    source_end_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> "SegmentCreate":
        if self.end_seconds < self.start_seconds:
            raise ValueError("end_seconds must be greater than or equal to start_seconds")
        self._validate_source_ranges()
        return self

    def _validate_source_ranges(self) -> None:
        if (
            self.source_page_start is not None
            and self.source_page_end is not None
            and self.source_page_end < self.source_page_start
        ):
            raise ValueError("source_page_end must be greater than or equal to source_page_start")
        if (
            self.source_slide_start is not None
            and self.source_slide_end is not None
            and self.source_slide_end < self.source_slide_start
        ):
            raise ValueError("source_slide_end must be greater than or equal to source_slide_start")
        if (
            self.source_start_seconds is not None
            and self.source_end_seconds is not None
            and self.source_end_seconds < self.source_start_seconds
        ):
            raise ValueError("source_end_seconds must be greater than or equal to source_start_seconds")


class ChunkCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

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
    source_type: SourceRangeType | None = None
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)
    source_slide_start: int | None = Field(default=None, ge=1)
    source_slide_end: int | None = Field(default=None, ge=1)
    source_start_seconds: float | None = Field(default=None, ge=0)
    source_end_seconds: float | None = Field(default=None, ge=0)

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
        self._validate_source_ranges()
        return self

    def _validate_source_ranges(self) -> None:
        if (
            self.source_page_start is not None
            and self.source_page_end is not None
            and self.source_page_end < self.source_page_start
        ):
            raise ValueError("source_page_end must be greater than or equal to source_page_start")
        if (
            self.source_slide_start is not None
            and self.source_slide_end is not None
            and self.source_slide_end < self.source_slide_start
        ):
            raise ValueError("source_slide_end must be greater than or equal to source_slide_start")
        if (
            self.source_start_seconds is not None
            and self.source_end_seconds is not None
            and self.source_end_seconds < self.source_start_seconds
        ):
            raise ValueError("source_end_seconds must be greater than or equal to source_start_seconds")


# chunks 테이블에서 읽어온 chunk 행을 나타내는 읽기 전용 모델.
# insert_chunks() 완료 후 DB에서 조회해 parent_chunk_id를 얻기 위해 사용한다.
class ChunkRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    chunk_index: int
    topic: str | None = None
    subtopic: str | None = None
    keywords: list[str] = Field(default_factory=list)
    speaker_labels: list[str] = Field(default_factory=list)
    segment_start_index: int | None
    segment_end_index: int | None
    start_seconds: float | None
    end_seconds: float | None
    text: str
    summary: str | None = None
    metadata: dict[str, Any]
    source_type: SourceRangeType | None = None
    source_page_start: int | None = None
    source_page_end: int | None = None
    source_slide_start: int | None = None
    source_slide_end: int | None = None
    source_start_seconds: float | None = None
    source_end_seconds: float | None = None


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
    source_type: SourceRangeType | None = None
    source_page_start: int | None = Field(default=None, ge=1)
    source_page_end: int | None = Field(default=None, ge=1)
    source_slide_start: int | None = Field(default=None, ge=1)
    source_slide_end: int | None = Field(default=None, ge=1)
    source_start_seconds: float | None = Field(default=None, ge=0)
    source_end_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_source_ranges(self) -> "SearchChunkCreate":
        if (
            self.source_page_start is not None
            and self.source_page_end is not None
            and self.source_page_end < self.source_page_start
        ):
            raise ValueError("source_page_end must be greater than or equal to source_page_start")
        if (
            self.source_slide_start is not None
            and self.source_slide_end is not None
            and self.source_slide_end < self.source_slide_start
        ):
            raise ValueError("source_slide_end must be greater than or equal to source_slide_start")
        if (
            self.source_start_seconds is not None
            and self.source_end_seconds is not None
            and self.source_end_seconds < self.source_start_seconds
        ):
            raise ValueError("source_end_seconds must be greater than or equal to source_start_seconds")
        return self


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
    source_type: SourceRangeType | None = None
    source_page_start: int | None = None
    source_page_end: int | None = None
    source_slide_start: int | None = None
    source_slide_end: int | None = None
    source_start_seconds: float | None = None
    source_end_seconds: float | None = None


# RAG 검색 시 상위 청크(parent chunk)의 전체 문맥을 반환하는 모델.
# 검색 히트된 child chunk의 parent를 조회하여 풍부한 메타데이터와 함께 응답한다.
class ParentChunkResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    transcript_id: UUID
    transcript_title: str | None = None
    chunk_index: int
    topic: str | None
    subtopic: str | None
    keywords: list[str]
    speaker_labels: list[str]
    segment_start_index: int | None = None
    segment_end_index: int | None = None
    start_seconds: float | None
    end_seconds: float | None
    text: str
    summary: str | None
    metadata: dict[str, Any]
    source_type: SourceRangeType | None = None
    source_page_start: int | None = None
    source_page_end: int | None = None
    source_slide_start: int | None = None
    source_slide_end: int | None = None
    source_start_seconds: float | None = None
    source_end_seconds: float | None = None


# transcripts 테이블에서 id로 단건 조회한 읽기 전용 모델.
# 요약 PDF 생성의 입력(full_text + 메타)을 담으며, get_transcript_by_id()가 반환한다.
class TranscriptDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: UUID | None
    title: str | None
    full_text: str | None
    summary: str | None
    duration_seconds: float | None
    language: str | None
    status: str
    created_at: Any | None = None


class UploadedFileDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    transcript_id: UUID
    title: str | None = None
    file_uri: str
    original_filename: str | None = None
    mime_type: str | None = None
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


# lecture_summaries 테이블 insert용 모델.
# 강의 요약 데이터 API가 생성한 overview/contexts/keywords JSON payload를 저장한다.
class LectureSummaryCreate(BaseModel):
    model_config = ConfigDict(frozen=True)

    transcript_id: UUID
    user_id: UUID | None = None
    payload: dict[str, Any]
    model: str | None = None


# lecture_summaries 테이블에서 읽어온 강의 요약 데이터 모델.
# 이미 생성된 요약이 있으면 LLM 재호출 없이 이 모델을 응답으로 변환한다.
class LectureSummaryDetail(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    transcript_id: UUID
    user_id: UUID | None
    payload: dict[str, Any]
    model: str | None


# 강의 요약 전체 개요 모델.
# API 응답 최상위 overview에 노출되어 프론트가 title/summary/key_points를 바로 사용할 수 있다.
class LectureSummaryOverview(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str = ""
    summary: str = ""
    key_points: list[str] = Field(default_factory=list)


# 강의 맥락 단위 요약 모델.
# chunk의 topic/keywords/concepts/learning_points와 시간 범위를 함께 노출한다.
class LectureSummaryContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    index: int
    topic: str = ""
    subtitle: str = ""
    content: str = ""
    keywords: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    learning_points: list[str] = Field(default_factory=list)
    start_seconds: float | None = None
    end_seconds: float | None = None
    segment_start_index: int | None = None
    segment_end_index: int | None = None


# 강의 키워드별 요약 모델.
# related_context_indices로 어떤 contexts와 연결되는지 표현한다.
class LectureSummaryKeyword(BaseModel):
    model_config = ConfigDict(frozen=True)

    keyword: str
    summary: str = ""
    related_context_indices: list[int] = Field(default_factory=list)


# lecture_summaries.payload JSONB에 저장되는 내부 payload 형태.
# DB에는 이 구조를 dict로 저장하고, API 응답에서는 payload 래퍼 없이 펼쳐서 반환한다.
class LectureSummaryPayload(BaseModel):
    model_config = ConfigDict(frozen=True)

    overview: LectureSummaryOverview = Field(default_factory=LectureSummaryOverview)
    contexts: list[LectureSummaryContext] = Field(default_factory=list)
    keywords: list[LectureSummaryKeyword] = Field(default_factory=list)


# POST /audio/transcripts/{transcript_id}/summary 응답 모델.
# payload 래퍼 없이 overview, contexts, keywords를 최상위에 둔다.
class LectureSummaryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    summary_id: UUID
    transcript_id: UUID
    persona_id: str = "general"
    overview: LectureSummaryOverview
    contexts: list[LectureSummaryContext]
    keywords: list[LectureSummaryKeyword]


# RAG 응답에서 클라이언트에 노출할 단일 출처 모델.
# DB 내부 chunk row 대신 문서/웹 공통으로 확장 가능한 얇은 source 형태를 사용한다.
class RetrievedSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_type: Literal["document", "web"] = "document"
    title: str
    snippet: str
    transcript_id: UUID | None = None
    url: str | None = None
    score: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


# POST /rag/query 엔드포인트 요청 모델.
# 인증 사용자의 transcript_ids 범위 안에서만 문서 검색을 수행한다.
class RagQueryRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    query: str = Field(min_length=1)
    scope: RagSearchScope = "document"
    transcript_ids: list[UUID] = Field(default_factory=list)
    top_k: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_scope_inputs(self) -> "RagQueryRequest":
        if self.scope in {"document", "hybrid"} and not self.transcript_ids:
            raise ValueError("transcript_ids is required for document and hybrid scope")
        return self


# POST /rag/query 엔드포인트 응답 모델.
# LLM이 생성한 answer와 클라이언트용 RetrievedSource 목록을 함께 반환한다.
class RagQueryResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    answer: str
    sources: list[RetrievedSource]
    warnings: list[str] = Field(default_factory=list)
