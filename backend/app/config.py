from functools import lru_cache
from pathlib import Path
import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_DIR = Path(__file__).resolve().parents[2]

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAPER_READER_", extra="ignore")
    project_dir: Path = PROJECT_DIR
    runtime_dir: Path = PROJECT_DIR.parent / "paper-reader-runtime"
    database_url: str
    encryption_key: str
    allow_private_models: bool = False
    pi_agent_enabled: bool = True
    lease_seconds: int = Field(default=60, ge=5)
    job_timeout_seconds: int = Field(default=1200, ge=10)
    max_attempts: int = Field(default=2, ge=1)
    max_tool_calls: int = Field(default=160, ge=10, le=500)
    long_job_timeout_seconds: int = Field(default=2400, ge=10, le=7200)
    long_max_tool_calls: int = Field(default=240, ge=10, le=500)
    segment_concurrency: int = Field(default=2, ge=1, le=2)
    max_running_jobs: int = Field(default=2, ge=1)
    max_jobs_per_workspace: int = Field(default=1, ge=1)
    secure_cookie: bool = False
    session_hours: int = 24
    upload_max_bytes: int = 50 * 1024 * 1024
    public_origin: str | None = None

@lru_cache
def get_settings() -> Settings:
    runtime = Path(os.environ.get("PAPER_READER_RUNTIME_DIR", PROJECT_DIR.parent / "paper-reader-runtime")).expanduser().resolve()
    settings = Settings(_env_file=runtime / "config" / "app.env", _env_file_encoding="utf-8")
    settings.runtime_dir = settings.runtime_dir.expanduser().resolve()
    if not settings.database_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise ValueError("PAPER_READER_DATABASE_URL must use PostgreSQL")
    return settings
