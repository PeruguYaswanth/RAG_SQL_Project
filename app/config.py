import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql://postgres:postgres@localhost:5432/rag_sql_db"
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "openai/gpt-oss-120b"
    MAX_UPLOAD_MB: int = 10
    CSV_CHUNK_THRESHOLD_MB: int = 2
    BATCH_SIZE: int = 500
    SESSION_TTL_SECONDS: int = 3600
    STATEMENT_TIMEOUT_MS: int = 5000

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
