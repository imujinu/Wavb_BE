from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field("", alias="DATABASE_URL")
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    openai_stt_model: str = Field("whisper-1", alias="OPENAI_STT_MODEL")
    openai_summary_model: str = Field("gpt-4o-mini", alias="OPENAI_SUMMARY_MODEL")
    openai_embedding_model: str = Field(
        "text-embedding-3-small",
        alias="OPENAI_EMBEDDING_MODEL",
    )
    audio_transcription_concurrency: int = Field(
        3,
        alias="AUDIO_TRANSCRIPTION_CONCURRENCY",
    )
    audio_chunk_min_seconds: int = Field(300, alias="AUDIO_CHUNK_MIN_SECONDS")
    audio_chunk_max_seconds: int = Field(900, alias="AUDIO_CHUNK_MAX_SECONDS")
    audio_chunk_overlap_seconds: int = Field(
        2,
        alias="AUDIO_CHUNK_OVERLAP_SECONDS",
    )
    audio_target_chunk_max_mb: int = Field(24, alias="AUDIO_TARGET_CHUNK_MAX_MB")
    audio_sync_timeout_budget_seconds: int = Field(
        110,
        alias="AUDIO_SYNC_TIMEOUT_BUDGET_SECONDS",
    )
    summary_text_chunk_chars: int = Field(
        16000,
        alias="SUMMARY_TEXT_CHUNK_CHARS",
    )
    summary_concurrency: int = Field(2, alias="SUMMARY_CONCURRENCY")
    allowed_origins_raw: str = Field(
        "http://localhost:8081,http://localhost:19006",
        alias="ALLOWED_ORIGINS",
    )
    # RAG 챗봇 설정: 검색 및 답변 생성 관련 파라미터
    rag_search_top_k: int = Field(12, alias="RAG_SEARCH_TOP_K")
    rag_parent_top_k: int = Field(5, alias="RAG_PARENT_TOP_K")
    rag_min_confidence: float = Field(0.35, alias="RAG_MIN_CONFIDENCE")
    rag_max_context_chars: int = Field(6000, alias="RAG_MAX_CONTEXT_CHARS")
    web_search_api_key: str = Field("", alias="WEB_SEARCH_API_KEY")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.allowed_origins_raw.split(",")
            if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
