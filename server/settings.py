from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    openai_api_key: str = Field("", alias="OPENAI_API_KEY")
    openai_stt_model: str = Field("whisper-1", alias="OPENAI_STT_MODEL")
    openai_summary_model: str = Field("gpt-4o-mini", alias="OPENAI_SUMMARY_MODEL")
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
