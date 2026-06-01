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
    # 템플릿 기반 요약 PDF 생성에 사용하는 설정값.
    # summary_pdf_model: 요약 품질/비용 조정용 모델 (기존 요약 모델과 동일 기본값)
    # summary_pdf_max_input_chars: 단일 LLM 호출로 처리할 입력 상한 (초과 시 앞부분만 사용)
    # summary_pdf_font_path: 한글 글꼴 경로 (빈 값이면 번들 기본 글꼴 사용)
    summary_pdf_model: str = Field("gpt-4o-mini", alias="SUMMARY_PDF_MODEL")
    summary_pdf_max_input_chars: int = Field(
        48000,
        alias="SUMMARY_PDF_MAX_INPUT_CHARS",
    )
    summary_pdf_font_path: str = Field("", alias="SUMMARY_PDF_FONT_PATH")
    allowed_origins_raw: str = Field(
        "http://localhost:8081,http://localhost:19006",
        alias="ALLOWED_ORIGINS",
    )
    # JWT 인증에 필요한 시크릿 키 및 토큰 만료 설정
    jwt_secret_key: str = Field("", alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field("HS256", alias="JWT_ALGORITHM")
    jwt_access_token_expire_minutes: int = Field(60, alias="JWT_ACCESS_TOKEN_EXPIRE_MINUTES")
    jwt_refresh_token_expire_days: int = Field(30, alias="JWT_REFRESH_TOKEN_EXPIRE_DAYS")

    # Google OAuth (console.cloud.google.com)
    google_client_id: str = Field("", alias="GOOGLE_CLIENT_ID")
    google_client_secret: str = Field("", alias="GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = Field("", alias="GOOGLE_REDIRECT_URI")

    # Kakao OAuth (developers.kakao.com — REST API 키)
    kakao_client_id: str = Field("", alias="KAKAO_CLIENT_ID")
    kakao_client_secret: str = Field("", alias="KAKAO_CLIENT_SECRET")
    kakao_redirect_uri: str = Field("", alias="KAKAO_REDIRECT_URI")

    # Naver OAuth (developers.naver.com)
    naver_client_id: str = Field("", alias="NAVER_CLIENT_ID")
    naver_client_secret: str = Field("", alias="NAVER_CLIENT_SECRET")
    naver_redirect_uri: str = Field("", alias="NAVER_REDIRECT_URI")

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
