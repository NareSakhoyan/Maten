from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


BACKEND_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = BACKEND_ROOT / "config"
LOCAL_ENV_FILE = BACKEND_ROOT / ".env"


def _selected_app_env() -> str:
    app_env = os.getenv("APP_ENV")
    if not app_env and LOCAL_ENV_FILE.exists():
        app_env = dotenv_values(LOCAL_ENV_FILE).get("APP_ENV")
    return (app_env or "development").strip().lower()


def settings_env_files() -> tuple[str, ...]:
    app_env = _selected_app_env()
    candidates = (
        CONFIG_DIR / "base.env",
        CONFIG_DIR / f"{app_env}.env",
        LOCAL_ENV_FILE,
    )
    return tuple(str(path) for path in candidates if path.exists())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    request_slow_info_ms: int = Field(default=500, alias="REQUEST_SLOW_INFO_MS", ge=0)
    request_slow_warning_ms: int = Field(default=1500, alias="REQUEST_SLOW_WARNING_MS", ge=0)
    request_slow_error_ms: int = Field(default=5000, alias="REQUEST_SLOW_ERROR_MS", ge=0)
    sql_timing_enabled: bool = Field(default=True, alias="SQL_TIMING_ENABLED")
    sql_slow_query_ms: int = Field(default=200, alias="SQL_SLOW_QUERY_MS", ge=0)
    otel_enabled: bool = Field(default=False, alias="OTEL_ENABLED")
    otel_service_name: str = Field(default="baghramyan-backend", alias="OTEL_SERVICE_NAME")
    api_host: str = Field(default="0.0.0.0", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")
    cors_allow_origins: str = Field(
        default="http://localhost:3000,http://127.0.0.1:3000",
        alias="CORS_ALLOW_ORIGINS",
    )
    cors_allow_origin_regex: str = Field(
        default=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        alias="CORS_ALLOW_ORIGIN_REGEX",
    )

    database_url: str = Field(alias="DATABASE_URL")
    database_pool_pre_ping: bool = Field(default=True, alias="DATABASE_POOL_PRE_PING")
    database_pool_recycle_seconds: int = Field(default=1800, alias="DATABASE_POOL_RECYCLE_SECONDS", ge=-1)

    supabase_url: str = Field(alias="SUPABASE_URL")
    supabase_publishable_key: str = Field(default="", alias="SUPABASE_PUBLISHABLE_KEY")
    supabase_service_role_key: str = Field(alias="SUPABASE_SERVICE_ROLE_KEY")
    supabase_auth_timeout_seconds: float = Field(
        default=5.0,
        alias="SUPABASE_AUTH_TIMEOUT_SECONDS",
        gt=0,
    )
    supabase_auth_cache_ttl_seconds: int = Field(
        default=60,
        alias="SUPABASE_AUTH_CACHE_TTL_SECONDS",
        ge=0,
    )
    supabase_bucket_book_originals: str = Field(
        default="book-originals",
        alias="SUPABASE_BUCKET_BOOK_ORIGINALS",
    )
    supabase_bucket_page_images: str = Field(
        default="page-images",
        alias="SUPABASE_BUCKET_PAGE_IMAGES",
    )
    supabase_bucket_ocr_json: str = Field(
        default="ocr-json",
        alias="SUPABASE_BUCKET_OCR_JSON",
    )

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    celery_broker_url: str = Field(default="redis://localhost:6379/0", alias="CELERY_BROKER_URL")
    celery_result_backend: str = Field(
        default="redis://localhost:6379/1",
        alias="CELERY_RESULT_BACKEND",
    )
    celery_worker_pool: str = Field(default="prefork", alias="CELERY_WORKER_POOL")
    celery_worker_concurrency: int = Field(default=4, alias="CELERY_WORKER_CONCURRENCY", ge=2)
    celery_task_heartbeat_seconds: int = Field(default=15, alias="CELERY_TASK_HEARTBEAT_SECONDS", ge=0)
    celery_queue_ingestion_concurrency: int = Field(default=2, alias="CELERY_QUEUE_INGESTION_CONCURRENCY", ge=1)
    celery_queue_ocr_concurrency: int = Field(default=1, alias="CELERY_QUEUE_OCR_CONCURRENCY", ge=1)
    celery_queue_nlp_concurrency: int = Field(default=1, alias="CELERY_QUEUE_NLP_CONCURRENCY", ge=1)
    celery_queue_discovery_concurrency: int = Field(default=2, alias="CELERY_QUEUE_DISCOVERY_CONCURRENCY", ge=1)
    celery_queue_external_concurrency: int = Field(default=1, alias="CELERY_QUEUE_EXTERNAL_CONCURRENCY", ge=1)
    celery_queue_evidence_concurrency: int = Field(default=2, alias="CELERY_QUEUE_EVIDENCE_CONCURRENCY", ge=1)
    max_active_jobs_per_user: int = Field(default=3, alias="MAX_ACTIVE_JOBS_PER_USER", ge=1)
    max_active_ocr_jobs_global: int = Field(default=1, alias="MAX_ACTIVE_OCR_JOBS_GLOBAL", ge=1)
    max_active_external_lookups_global: int = Field(default=1, alias="MAX_ACTIVE_EXTERNAL_LOOKUPS_GLOBAL", ge=1)
    external_lookup_stale_job_minutes: int = Field(default=30, alias="EXTERNAL_LOOKUP_STALE_JOB_MINUTES", ge=1)

    tessdata_prefix: str | None = Field(default=None, alias="TESSDATA_PREFIX")
    tesseract_lang: str = Field(default="hye-calfa-n", alias="TESSERACT_LANG")
    ocr_dpi: int = Field(default=300, alias="OCR_DPI")
    max_upload_mb: int = Field(default=100, alias="MAX_UPLOAD_MB")
    reference_import_max_line_length: int = Field(default=120, alias="REFERENCE_IMPORT_MAX_LINE_LENGTH")
    reference_pdf_text_min_length: int = Field(default=40, alias="REFERENCE_PDF_TEXT_MIN_LENGTH")
    reference_fuzzy_threshold_default: int = Field(default=90, alias="REFERENCE_FUZZY_THRESHOLD_DEFAULT")
    external_lookup_enabled: bool = Field(default=True, alias="EXTERNAL_LOOKUP_ENABLED")
    external_lookup_cache_ttl_hours: int = Field(
        default=24,
        alias="EXTERNAL_LOOKUP_CACHE_TTL_HOURS",
        ge=0,
    )
    external_lookup_http_timeout_seconds: float = Field(
        default=10.0,
        alias="EXTERNAL_LOOKUP_HTTP_TIMEOUT_SECONDS",
        gt=0,
    )
    nayiri_provider_enabled: bool = Field(default=True, alias="NAYIRI_PROVIDER_ENABLED")
    nayiri_provider_base_url: str = Field(
        default="http://www.nayiri.com/search",
        alias="NAYIRI_PROVIDER_BASE_URL",
    )
    nayiri_provider_rate_limit_ms: int = Field(
        default=500,
        alias="NAYIRI_PROVIDER_RATE_LIMIT_MS",
        ge=0,
    )
    pie_enabled: bool = Field(default=True, alias="PIE_ENABLED")
    pie_executable: str = Field(default="pie", alias="PIE_EXECUTABLE")
    pie_model_root: str | None = Field(default=None, alias="PIE_MODEL_ROOT")
    pie_eastern_model_path: str | None = Field(default=None, alias="PIE_EASTERN_MODEL_PATH")
    pie_classical_model_path: str | None = Field(default=None, alias="PIE_CLASSICAL_MODEL_PATH")
    pie_default_profile: str = Field(default="classical", alias="PIE_DEFAULT_PROFILE")
    pie_model_key: str = Field(default="xcl", alias="PIE_MODEL_KEY")
    pie_batch_size: int = Field(default=8, alias="PIE_BATCH_SIZE", ge=1)
    pie_max_tokens_per_batch: int = Field(default=256, alias="PIE_MAX_TOKENS_PER_BATCH", ge=1)
    pie_timeout_seconds: float = Field(default=30.0, alias="PIE_TIMEOUT_SECONDS", gt=0)
    pie_run_only_for_classical: bool = Field(default=True, alias="PIE_RUN_ONLY_FOR_CLASSICAL")
    resource_manifest_path: str | None = Field(default=None, alias="RESOURCE_MANIFEST_PATH")

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str) -> str:
        if not isinstance(value, str):
            return value

        normalized = value.strip().strip('"').strip("'")
        if normalized.startswith("postgres://"):
            return f"postgresql+psycopg://{normalized[len('postgres://'):]}"
        if normalized.startswith("postgresql://") and "+" not in normalized.split("://", maxsplit=1)[0]:
            return f"postgresql+psycopg://{normalized[len('postgresql://'):]}"
        return normalized

    @field_validator("supabase_url", mode="before")
    @classmethod
    def normalize_supabase_url(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().strip('"').strip("'").rstrip("/")

    @field_validator("nayiri_provider_base_url", mode="before")
    @classmethod
    def normalize_nayiri_base_url(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        return value.strip().strip('"').strip("'").rstrip("/")

    @field_validator("pie_model_root", mode="before")
    @classmethod
    def normalize_pie_model_root(cls, value: str | None) -> str | None:
        if not isinstance(value, str):
            return value
        normalized = value.strip().strip('"').strip("'")
        return normalized or None

    @field_validator("pie_eastern_model_path", "pie_classical_model_path", mode="before")
    @classmethod
    def normalize_optional_path(cls, value: str | None) -> str | None:
        if not isinstance(value, str):
            return value
        normalized = value.strip().strip('"').strip("'")
        return normalized or None

    @field_validator("pie_default_profile", mode="before")
    @classmethod
    def normalize_pie_default_profile(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        normalized = value.strip().strip('"').strip("'").lower()
        return normalized or "classical"

    @field_validator("pie_executable", mode="before")
    @classmethod
    def normalize_pie_executable(cls, value: str) -> str:
        if not isinstance(value, str):
            return value
        normalized = value.strip().strip('"').strip("'")
        return normalized or "pie"

    @field_validator("resource_manifest_path", mode="before")
    @classmethod
    def normalize_resource_manifest_path(cls, value: str | None) -> str | None:
        if not isinstance(value, str):
            return value
        normalized = value.strip().strip('"').strip("'")
        return normalized or None

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def cors_allow_origins_list(self) -> list[str]:
        return [
            origin.strip()
            for origin in self.cors_allow_origins.split(",")
            if origin.strip()
        ]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(_env_file=settings_env_files())
