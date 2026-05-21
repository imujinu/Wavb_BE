from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    openai_stt_model: str = Field("whisper-1", alias="OPENAI_STT_MODEL")
    openai_summary_model: str = Field("gpt-4o-mini", alias="OPENAI_SUMMARY_MODEL")
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
