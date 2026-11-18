"""
QuizFlow FastAPI Main Application.
Production-hardened with:
- Structured logging (text or JSON)
- GZip response compression
- Request payload size protection (HTTP 413)
- Distributed Redis/local sliding-window rate limiting (HTTP 429)
- Per-request latency, DB query count, and Prometheus/JSON performance monitoring
- Security response headers & safe CORS configuration
- Health & Readiness probes + graceful DB pool shutdown
"""
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.api import api_router
from app.core.cache import cache_manager
from app.core.config import settings
from app.core.monitoring import (
    configure_logging,
    get_request_query_stats,
    metrics_collector,
    reset_request_query_stats,
)
from app.core.rate_limit import rate_limiter
from app.database.init_db import bootstrap_admin, init_db
from app.database.session import SessionLocal, engine

configure_logging()
logger = logging.getLogger("quizflow.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Initializing QuizFlow database (env=%s, dialect=%s)...", settings.ENVIRONMENT, engine.dialect.name)
    init_db()
    if settings.ENVIRONMENT != "testing":
        bootstrap_admin()
    logger.info(
        "QuizFlow v%s backend initialized (cache=%s, rate_limit=%s).",
        settings.VERSION,
        cache_manager.stats().get("backend"),
        settings.RATE_LIMIT_ENABLED,
    )
    yield
    logger.info("QuizFlow backend shutting down gracefully; disposing DB engine pool...")
    engine.dispose()


app = FastAPI(
    title=settings.PROJECT_NAME,
    description="High-Concurrency Online Quiz & Assessment Platform API",
    version=settings.VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url=f"{settings.API_V1_STR}/openapi.json",
    lifespan=lifespan,
)

# GZip compression for large responses (reports, question payloads, paginated lists)
app.add_middleware(GZipMiddleware, minimum_size=1024)

# Safe CORS Configuration
origins = settings.BACKEND_CORS_ORIGINS
if "*" in origins and settings.ENVIRONMENT == "production":
    # Browsers forbid credentials with wildcard origin; warn and disallow credentials with wildcard in prod
    logger.warning("Wildcard CORS origin '*' configured in production; disabling allow_credentials for safety.")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count", "X-Limit", "X-Offset", "X-Has-More", "X-Next-Cursor", "X-Request-ID"],
    )
else:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if "*" in origins else origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count", "X-Limit", "X-Offset", "X-Has-More", "X-Next-Cursor", "X-Request-ID"],
    )


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit_bucket(path: str) -> tuple[str, int]:
    if path.startswith(f"{settings.API_V1_STR}/auth/"):
        return "auth", settings.RATE_LIMIT_AUTH_RPM
    if path.startswith(f"{settings.API_V1_STR}/attempts/"):
        return "attempts", settings.RATE_LIMIT_ATTEMPT_RPM
    return "default", settings.RATE_LIMIT_DEFAULT_RPM


@app.middleware("http")
async def security_monitoring_and_rate_limit(request: Request, call_next):
    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    reset_request_query_stats(req_id)
    start_time = time.perf_counter()
    path = request.url.path

    # 1. Request Payload Size Guard (HTTP 413)
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    content={"detail": f"Request payload exceeds maximum limit of {settings.MAX_REQUEST_BODY_BYTES} bytes."},
                )
        except ValueError:
            pass

    # 2. Rate Limiting (skip health/ready/docs endpoints)
    if settings.RATE_LIMIT_ENABLED and path not in ("/", "/health", "/ready", f"{settings.API_V1_STR}/health", f"{settings.API_V1_STR}/ready"):
        bucket, rpm = _rate_limit_bucket(path)
        allowed, remaining, reset_sec = rate_limiter.check(bucket, _client_ip(request), rpm)
        if not allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "Too many requests. Please slow down and try again shortly."},
                headers={
                    "Retry-After": str(reset_sec),
                    "X-RateLimit-Limit": str(rpm),
                    "X-RateLimit-Remaining": "0",
                    "X-Request-ID": req_id,
                },
            )

    response = await call_next(request)
    duration_ms = (time.perf_counter() - start_time) * 1000.0
    q_count, q_time_ms = get_request_query_stats()

    # Resolve route template for low-cardinality metrics aggregation
    route_obj = request.scope.get("route")
    route_label = f"{request.method} {getattr(route_obj, 'path', path)}"

    if settings.METRICS_ENABLED:
        metrics_collector.record_request(
            route=route_label,
            status_code=response.status_code,
            duration_ms=duration_ms,
            query_count=q_count,
        )

    # Security & diagnostics headers (never expose secrets or internals)
    response.headers["X-Request-ID"] = req_id
    response.headers["X-Process-Time-Ms"] = f"{duration_ms:.2f}"
    response.headers["X-DB-Query-Count"] = str(q_count)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    logger.info(
        "%s %s -> %d (%.2fms | db_queries=%d db_ms=%.2f req_id=%s)",
        request.method,
        path,
        response.status_code,
        duration_ms,
        q_count,
        q_time_ms,
        req_id,
    )
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    errors = exc.errors()
    first_error = errors[0]["msg"] if errors else "Validation failed"
    safe_errors = [
        {
            "type": err.get("type", "value_error"),
            "loc": err.get("loc", []),
            "msg": err.get("msg", ""),
        }
        for err in errors
    ]
    return JSONResponse(
        status_code=422,
        content={"detail": first_error, "errors": safe_errors},
    )


@app.get("/", tags=["Root"])
def root():
    return {
        "app": settings.PROJECT_NAME,
        "version": settings.VERSION,
        "status": "online",
        "docs": "/docs",
        "api_v1": settings.API_V1_STR,
    }


@app.get("/health", tags=["Health"])
def direct_health():
    db = SessionLocal()
    db_status = "healthy"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"
    finally:
        db.close()

    return {
        "status": "online" if db_status == "healthy" else "degraded",
        "database": db_status,
        "cache_backend": cache_manager.stats().get("backend"),
        "app": settings.PROJECT_NAME,
        "version": settings.VERSION,
    }


@app.get("/ready", tags=["Health"])
def direct_readiness(response: Response):
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"ready": True, "database": "healthy"}
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"ready": False, "database": f"unhealthy: {exc}"}
    finally:
        db.close()


# Include API V1 Routers
app.include_router(api_router, prefix=settings.API_V1_STR)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
