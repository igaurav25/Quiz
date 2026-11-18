"""
Database Engine, Connection Pooling, Concurrency Tuning, Retry Logic, and Query Monitoring.
Supports:
- SQLite (local development): WAL mode, NORMAL synchronous, foreign_keys=ON, busy_timeout, memory temp_store.
- PostgreSQL (production / high concurrency): Configurable QueuePool (or NullPool behind PgBouncer),
  pool_pre_ping, connection & statement timeouts, and transient error retry helpers.
"""
import logging
import time
from typing import Any, Callable, Dict, Generator, TypeVar

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DisconnectionError, OperationalError
from sqlalchemy.orm import Session, declarative_base, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool, StaticPool

from app.core.config import settings
from app.core.monitoring import record_db_query

logger = logging.getLogger("quizflow.database")

T = TypeVar("T")


def _build_engine() -> Engine:
    url = settings.DATABASE_URL

    if url.startswith("sqlite"):
        is_memory = ":memory:" in url or "mode=memory" in url
        connect_args: Dict[str, Any] = {
            "check_same_thread": False,
            "timeout": max(5, settings.SQLITE_BUSY_TIMEOUT_MS // 1000),
        }
        engine_kwargs: Dict[str, Any] = {
            "connect_args": connect_args,
            "echo": settings.DB_ECHO,
            "future": True,
        }
        if is_memory:
            engine_kwargs["poolclass"] = StaticPool
        else:
            # QueuePool allows concurrent SQLite readers under WAL mode
            engine_kwargs["poolclass"] = QueuePool
            engine_kwargs["pool_size"] = min(settings.DB_POOL_SIZE, 20)
            engine_kwargs["max_overflow"] = min(settings.DB_MAX_OVERFLOW, 30)
            engine_kwargs["pool_timeout"] = settings.DB_POOL_TIMEOUT
            engine_kwargs["pool_pre_ping"] = settings.DB_POOL_PRE_PING

        return create_engine(url, **engine_kwargs)

    # PostgreSQL / other RDBMS production configuration
    connect_args = {}
    if url.startswith("postgresql") or url.startswith("postgres"):
        connect_args["connect_timeout"] = settings.DB_CONNECT_TIMEOUT
        if settings.DB_STATEMENT_TIMEOUT_MS > 0:
            connect_args["options"] = f"-c statement_timeout={settings.DB_STATEMENT_TIMEOUT_MS}"

    engine_kwargs = {
        "connect_args": connect_args,
        "pool_pre_ping": settings.DB_POOL_PRE_PING,
        "echo": settings.DB_ECHO,
        "future": True,
    }

    if settings.DB_USE_NULL_POOL:
        # Recommended when PgBouncer runs in transaction pooling mode
        engine_kwargs["poolclass"] = NullPool
    else:
        engine_kwargs["poolclass"] = QueuePool
        engine_kwargs["pool_size"] = settings.DB_POOL_SIZE
        engine_kwargs["max_overflow"] = settings.DB_MAX_OVERFLOW
        engine_kwargs["pool_timeout"] = settings.DB_POOL_TIMEOUT
        engine_kwargs["pool_recycle"] = settings.DB_POOL_RECYCLE

    return create_engine(url, **engine_kwargs)


engine = _build_engine()


def attach_engine_listeners(target_engine: Engine) -> None:
    """Attach SQLite PRAGMA tuning and SQL query performance instrumentation to an engine."""
    if getattr(target_engine, "_quizflow_listeners_attached", False):
        return

    if target_engine.dialect.name == "sqlite":
        @event.listens_for(target_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute(f"PRAGMA busy_timeout={settings.SQLITE_BUSY_TIMEOUT_MS}")
                cursor.execute(f"PRAGMA journal_mode={settings.SQLITE_JOURNAL_MODE}")
                cursor.execute(f"PRAGMA synchronous={settings.SQLITE_SYNCHRONOUS}")
                cursor.execute("PRAGMA temp_store=MEMORY")
                cursor.execute(f"PRAGMA cache_size=-{settings.SQLITE_CACHE_SIZE_KB}")
            except Exception as exc:
                logger.debug("SQLite pragma setup notice: %s", exc)
            finally:
                cursor.close()

    @event.listens_for(target_engine, "before_cursor_execute")
    def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        context._qf_query_start = time.perf_counter()

    @event.listens_for(target_engine, "after_cursor_execute")
    def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        start = getattr(context, "_qf_query_start", None)
        if start is None:
            return
        duration_ms = (time.perf_counter() - start) * 1000.0
        # Never log query parameters (could include emails/tokens/hashes)
        sql_summary = " ".join(statement.split())
        record_db_query(duration_ms, sql_summary)
        if duration_ms >= settings.SLOW_QUERY_THRESHOLD_MS:
            logger.warning("Slow SQL query (%.2fms): %s", duration_ms, sql_summary[:200])

    target_engine._quizflow_listeners_attached = True


attach_engine_listeners(engine)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db() -> Generator[Session, None, None]:
    """Dependency that yields a database session and safely closes it upon exit."""
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def execute_with_retry(
    db: Session,
    operation: Callable[[], T],
    max_attempts: int = settings.DB_RETRY_ATTEMPTS,
    base_delay: float = settings.DB_RETRY_DELAY_SECONDS,
) -> T:
    """
    Execute a transactional database operation with bounded exponential backoff retry
    for transient lock/disconnect errors (e.g., SQLite busy lock, PG serialization/connection drop).
    """
    last_exc: Exception | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        try:
            return operation()
        except (OperationalError, DisconnectionError) as exc:
            last_exc = exc
            db.rollback()
            msg = str(exc).lower()
            is_transient = any(
                token in msg
                for token in (
                    "locked",
                    "busy",
                    "deadlock",
                    "serialization failure",
                    "connection",
                    "server closed",
                    "could not serialize",
                )
            )
            if not is_transient or attempt >= max_attempts:
                raise
            sleep_for = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "Transient DB error on attempt %d/%d (%s). Retrying in %.2fs...",
                attempt,
                max_attempts,
                msg[:120],
                sleep_for,
            )
            time.sleep(sleep_for)
    if last_exc:
        raise last_exc
    raise RuntimeError("execute_with_retry exited unexpectedly")


def get_pool_stats() -> Dict[str, Any]:
    """Inspect current SQLAlchemy engine pool utilization safely."""
    pool = engine.pool
    stats: Dict[str, Any] = {
        "dialect": engine.dialect.name,
        "pool_class": type(pool).__name__,
    }
    for attr in ("size", "checkedin", "checkedout", "overflow"):
        fn = getattr(pool, attr, None)
        if callable(fn):
            try:
                stats[attr] = fn()
            except Exception:
                pass
    return stats
