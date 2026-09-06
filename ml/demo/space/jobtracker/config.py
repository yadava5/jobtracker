"""
Configuration Module
====================

Application settings and configuration management using pydantic-settings.

All settings can be overridden via environment variables with the
JOBTRACKER_ prefix. For example:
    JOBTRACKER_SYNC_BATCH_SIZE=250
    JOBTRACKER_LOG_LEVEL=DEBUG

Settings are loaded from:
1. Default values defined in this file
2. .env file in the backend directory (if exists)
3. Environment variables (highest priority)

Usage:
------
    from jobtracker.config import settings

    print(settings.sync_batch_size)  # 100
    print(settings.database_path)  # ~/Library/Application Support/JobTracker/jobtracker.db

Both examples name fields that something READS. That is not incidental: the
previous pair demonstrated ``api_port`` and ``api_host``, and both were deleted
in #645 as fields nothing consumed — a module docstring teaching an example
that the module no longer contains. ``tests/test_no_dead_settings_fields.py``
is what stops a field outliving its last reader; nothing stops a docstring
outliving its subject except reading it.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings with environment variable support.

    All settings can be overridden via environment variables
    prefixed with JOBTRACKER_.
    """

    model_config = SettingsConfigDict(
        env_prefix="JOBTRACKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -------------------------------------------------------------------------
    # Application
    # -------------------------------------------------------------------------
    app_name: str = "JobTracker"
    app_version: str = "0.1.0"
    # Environment:
    # - development: normal local runs (uses on-disk SQLite DB)
    # - production: same as development for now, but reserved for future tuning
    # - test: in-memory SQLite DB for pytest (never touches real data)
    environment: Literal["development", "production", "test"] = "development"

    # Deployment target. "desktop" keeps every existing assumption (SQLite,
    # Keychain, WebSocket router, localhost CORS). "cloud" selects the
    # Vercel-safe code paths (Postgres via DATABASE_URL, encrypted-column
    # credentials, polling, env-driven CORS). Downstream issues wire the
    # cloud paths in one at a time; this flag only gates which app builder
    # is imported.
    deployment: Literal["desktop", "cloud"] = "desktop"

    # -------------------------------------------------------------------------
    # Database
    # -------------------------------------------------------------------------
    database_dir: str = Field(
        default="~/Library/Application Support/JobTracker",
        description="Directory for SQLite database and related files",
    )
    database_name: str = "jobtracker.db"
    database_echo: bool = Field(
        default=False,
        description="Enable verbose SQL statement logging.",
    )
    database_url_override: str | None = Field(
        default=None,
        description=(
            "Explicit async DB URL (e.g. postgresql+asyncpg://... for Supabase "
            "or sqlite+aiosqlite:///path.db). When set, this overrides the "
            "computed SQLite URL used by the application engine. Leave unset "
            "on desktop builds to keep the local SQLite database."
        ),
    )

    @computed_field  # type: ignore[misc]
    @property
    def database_path(self) -> Path:
        """Full path to the SQLite database file."""
        return Path(self.database_dir).expanduser() / self.database_name

    @computed_field  # type: ignore[misc]
    @property
    def database_url(self) -> str:
        """SQLAlchemy async database URL.

        Resolution order:
        1. ``database_url_override`` - explicit opt-in for Postgres/Supabase.
        2. Test environment -> isolated in-memory SQLite.
        3. Desktop default -> on-disk SQLite at ``database_path``.
        """

        if self.database_url_override:
            return self.database_url_override

        # During tests we want a completely isolated, in-memory database that
        # does not touch the real on-disk JobTracker DB.
        if self.environment == "test":
            return "sqlite+aiosqlite:///:memory:"

        return f"sqlite+aiosqlite:///{self.database_path}"

    # -------------------------------------------------------------------------
    # Logging
    # -------------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_dir: str = Field(
        default="~/Library/Logs/JobTracker",
        description="Directory for log files",
    )
    uvicorn_access_log: bool = Field(
        default=False,
        description="Enable Uvicorn per-request access logging.",
    )
    log_format: str = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    log_date_format: str = "%Y-%m-%d %H:%M:%S"

    @computed_field  # type: ignore[misc]
    @property
    def log_path(self) -> Path:
        """Full path to the log directory."""
        return Path(self.log_dir).expanduser()

    # -------------------------------------------------------------------------
    # Email Sync
    # -------------------------------------------------------------------------
    sync_batch_size: int = Field(
        default=100,
        description="Number of emails to fetch per batch",
    )

    # -------------------------------------------------------------------------
    # Gmail API
    # -------------------------------------------------------------------------
    gmail_scopes: list[str] = Field(
        default=["https://www.googleapis.com/auth/gmail.readonly"],
        description="OAuth2 scopes for Gmail API access",
    )

    # -------------------------------------------------------------------------
    # ML Classifier
    # -------------------------------------------------------------------------
    ml_model_delivery_strategy: Literal[
        "download_on_first_launch", "bundle_in_app"
    ] = Field(
        default="download_on_first_launch",
        description=(
            "How ML models are delivered for desktop builds. "
            "'download_on_first_launch' keeps app size smaller and downloads models "
            "the first time classification is used."
        ),
    )
    lite_mode: bool = Field(
        default=False,
        description="Disable SetFit for 8GB RAM machines (rules + embeddings only)",
    )

    # -------------------------------------------------------------------------
    # Keychain
    # -------------------------------------------------------------------------
    keychain_service: str = "jobtracker"

    # -------------------------------------------------------------------------
    # Cloud (Vercel + Supabase). Only consumed when deployment == "cloud".
    # -------------------------------------------------------------------------
    cors_allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Extra hostnames permitted by CORS in cloud mode. Comma-separated "
            "in the env var, for example "
            "JOBTRACKER_CORS_ALLOWED_HOSTS='jobtracker.app,app.jobtracker.dev'. "
            "Vercel preview URLs (*.vercel.app) are always allowed."
        ),
    )
    supabase_jwt_secret: str | None = Field(
        default=None,
        description="Supabase JWT signing secret; required for cloud auth middleware (C3).",
    )
    secret_encryption_key: str | None = Field(
        default=None,
        description=(
            "Fernet key (urlsafe base64, 32 bytes) used to encrypt user credentials "
            "stored in the cloud `user_credentials` table (C4). Generate with "
            "`python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\"`."
        ),
    )
    vercel_cron_secret: str | None = Field(
        default=None,
        description=(
            "Shared secret Vercel Cron attaches via `x-vercel-cron-secret` header; "
            "used by `POST /cron/sync` (C7) to reject unauthenticated cron calls."
        ),
    )

    @field_validator("cors_allowed_hosts", mode="before")
    @classmethod
    def _split_cors_hosts(cls, value: Any) -> Any:
        """Accept a comma-separated env var string for cors_allowed_hosts.

        Vercel and most shells can only pass strings, so
        `JOBTRACKER_CORS_ALLOWED_HOSTS='jobtracker.app,app.jobtracker.dev'`
        should Just Work. A list/tuple is still accepted for programmatic use.
        """

        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    def ensure_directories(self) -> None:
        """Create required directories if they don't exist."""
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """
    Get cached application settings.

    Settings are loaded once and cached for performance.
    Use this function to access settings throughout the app.

    Returns:
        Settings: Application settings instance.
    """
    return Settings()


# Convenience alias for importing
settings = get_settings()
