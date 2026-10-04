from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-6-luna"
    api_key: SecretStr = SecretStr("")
    csv_path: Path = Path("data/faturas.csv")
    max_upload_mb: int = Field(default=20, ge=1, le=40)
    max_pages: int = Field(default=30, ge=1, le=200)
    openai_timeout_seconds: float = Field(default=120, gt=0)
    frontend_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.frontend_origins.split(",") if origin.strip()]
