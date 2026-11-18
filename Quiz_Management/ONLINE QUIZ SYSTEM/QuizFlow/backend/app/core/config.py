"""
QuizFlow Core Application Settings.
Supports loading from environment variables, .env file, and production-ready defaults.
Separates development (SQLite) and production (PostgreSQL + Redis + multi-worker) settings cleanly.
"""
import json
import os
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# Find .env relative to backend directory or working directory
BASE_DIR = Path(__file__).resolve().parent.parent.parent
env_path = BASE_DIR / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path)
else:
    load_dotenv()


def _bool_env(key: str, default: bool = False) -> bool:
    val = os.getenv(key)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "on")


def _int_env(key: str, default: int) -> int:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return int(val.strip())
    except ValueError:
        return default


def _float_env(key: str, default: float) -> float:
    val = os.getenv(key)
    if val is None:
        return default
    try:
        return float(val.strip())
    except ValueError:
        return default


class Settings:
    PROJECT_NAME: str = os.getenv("PROJECT_NAME", "QuizFlow")
    VERSION: str = os.getenv("APP_VERSION", "2.0.0")
    API_V1_STR: str = os.getenv("API_V1_STR", "/api/v1")
    ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development").strip().lower()
    DEBUG: bool = _bool_env("DEBUG", default=(ENVIRONMENT == "development"))

    # Security & Auth
    SECRET_KEY: str = os.getenv(
        "SECRET_KEY",
        "quizflow_super_secret_jwt_key_change_in_production_2026_at_least_32_chars",
    )
    ALGORITHM: str = os.getenv("ALGORITHM", "HS256")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = _int_env("ACCESS_TOKEN_EXPIRE_MINUTES", 1440)

    # Database Configuration (SQLite for local dev, PostgreSQL for production)
    DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'quizflow.db'}")
    DB_POOL_SIZE: int = _int_env("DB_POOL_SIZE", 20)
    DB_MAX_OVERFLOW: int = _int_env("DB_MAX_OVERFLOW", 30)
    DB_POOL_TIMEOUT: int = _int_env("DB_POOL_TIMEOUT", 30)
    DB_POOL_RECYCLE: int = _int_env("DB_POOL_RECYCLE", 1800)
    DB_POOL_PRE_PING: bool = _bool_env("DB_POOL_PRE_PING", True)
    DB_CONNECT_TIMEOUT: int = _int_env("DB_CONNECT_TIMEOUT", 10)
    DB_STATEMENT_TIMEOUT_MS: int = _int_env("DB_STATEMENT_TIMEOUT_MS", 30000)
    DB_USE_NULL_POOL: bool = _bool_env("DB_USE_NULL_POOL", False)  # Enable behind PgBouncer transaction pooling
    DB_ECHO: bool = _bool_env("DB_ECHO", False)
    DB_RETRY_ATTEMPTS: int = _int_env("DB_RETRY_ATTEMPTS", 3)
    DB_RETRY_DELAY_SECONDS: float = _float_env("DB_RETRY_DELAY_SECONDS", 0.15)

    # SQLite Concurrency Tuning
    SQLITE_JOURNAL_MODE: str = os.getenv("SQLITE_JOURNAL_MODE", "WAL")
    SQLITE_SYNCHRONOUS: str = os.getenv("SQLITE_SYNCHRONOUS", "NORMAL")
    SQLITE_BUSY_TIMEOUT_MS: int = _int_env("SQLITE_BUSY_TIMEOUT_MS", 15000)
    SQLITE_CACHE_SIZE_KB: int = _int_env("SQLITE_CACHE_SIZE_KB", 32768)

    # Redis Caching (Optional in local dev; recommended in production / multi-instance)
    REDIS_URL: str = os.getenv("REDIS_URL", "").strip()
    CACHE_ENABLED: bool = _bool_env("CACHE_ENABLED", True)
    CACHE_DEFAULT_TTL: int = _int_env("CACHE_DEFAULT_TTL", 60)
    CACHE_QUIZ_LIST_TTL: int = _int_env("CACHE_QUIZ_LIST_TTL", 60)
    CACHE_QUIZ_META_TTL: int = _int_env("CACHE_QUIZ_META_TTL", 120)
    CACHE_DASHBOARD_TTL: int = _int_env("CACHE_DASHBOARD_TTL", 30)
    CACHE_LOCAL_MAX_ENTRIES: int = _int_env("CACHE_LOCAL_MAX_ENTRIES", 512)
    REDIS_SOCKET_TIMEOUT: float = _float_env("REDIS_SOCKET_TIMEOUT", 1.5)

    # Background Tasks (Celery + Redis, with safe local BackgroundTasks fallback)
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "").strip() or REDIS_URL
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "").strip() or REDIS_URL
    BACKGROUND_TASKS_ENABLED: bool = _bool_env("BACKGROUND_TASKS_ENABLED", True)

    # Pagination
    DEFAULT_PAGE_SIZE: int = _int_env("DEFAULT_PAGE_SIZE", 50)
    MAX_PAGE_SIZE: int = _int_env("MAX_PAGE_SIZE", 200)

    # Quiz Attempt Timing & Integrity
    ATTEMPT_GRACE_PERIOD_SECONDS: int = _int_env("ATTEMPT_GRACE_PERIOD_SECONDS", 15)
    MAX_BATCH_ANSWERS: int = _int_env("MAX_BATCH_ANSWERS", 200)

    # Rate Limiting & Request Protection
    RATE_LIMIT_ENABLED: bool = _bool_env("RATE_LIMIT_ENABLED", default=(ENVIRONMENT != "testing"))
    RATE_LIMIT_DEFAULT_RPM: int = _int_env("RATE_LIMIT_DEFAULT_RPM", 600)
    RATE_LIMIT_AUTH_RPM: int = _int_env("RATE_LIMIT_AUTH_RPM", 60)
    RATE_LIMIT_ATTEMPT_RPM: int = _int_env("RATE_LIMIT_ATTEMPT_RPM", 1200)
    MAX_REQUEST_BODY_BYTES: int = _int_env("MAX_REQUEST_BODY_BYTES", 10 * 1024 * 1024)  # 10 MB

    # Monitoring & Logging
    METRICS_ENABLED: bool = _bool_env("METRICS_ENABLED", True)
    SLOW_QUERY_THRESHOLD_MS: float = _float_env("SLOW_QUERY_THRESHOLD_MS", 100.0)
    LOG_FORMAT: str = os.getenv("LOG_FORMAT", "text").strip().lower()  # 'text' or 'json'
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO" if not DEBUG else "INFO").upper()

    # Bootstrap Admin (Development / First-run convenience)
    FIRST_SUPERUSER_EMAIL: str = os.getenv("FIRST_SUPERUSER_EMAIL", "admin@quizflow.local")
    FIRST_SUPERUSER_PASSWORD: str = os.getenv("FIRST_SUPERUSER_PASSWORD", "AdminSecurePass2026!")
    FIRST_SUPERUSER_NAME: str = os.getenv("FIRST_SUPERUSER_NAME", "QuizFlow Administrator")

    # CORS
    _cors_raw: str = os.getenv(
        "BACKEND_CORS_ORIGINS",
        '["http://localhost:3000","http://localhost:8081","http://localhost:19006","http://localhost:8000","*"]',
    )

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def is_postgres(self) -> bool:
        return self.DATABASE_URL.startswith("postgresql") or self.DATABASE_URL.startswith("postgres")

    @property
    def BACKEND_CORS_ORIGINS(self) -> List[str]:
        try:
            origins = json.loads(self._cors_raw)
            if isinstance(origins, list):
                return [str(o) for o in origins]
            return [str(origins)]
        except Exception:
            return ["*"]


settings = Settings()
