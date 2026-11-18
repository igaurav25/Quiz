"""
Health Check, Readiness Probe, and System Diagnostics Endpoints.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.cache import cache_manager
from app.core.config import settings
from app.database.session import get_pool_stats

router = APIRouter()


@router.get("/health", summary="Perform system health check (liveness + dependency status)")
def health_check(db: Session = Depends(get_db)):
    db_status = "healthy"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        db_status = f"unhealthy: {str(e)}"

    redis_ping = cache_manager.ping_redis()
    redis_status = (
        "not_configured"
        if redis_ping is None
        else ("healthy" if redis_ping else "degraded_fallback_active")
    )

    return {
        "status": "online" if db_status == "healthy" else "degraded",
        "app_name": settings.PROJECT_NAME,
        "environment": settings.ENVIRONMENT,
        "database": db_status,
        "database_dialect": get_pool_stats().get("dialect"),
        "cache_backend": cache_manager.stats().get("backend"),
        "redis": redis_status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": settings.VERSION,
    }


@router.get("/ready", summary="Kubernetes / Load Balancer readiness probe")
def readiness_check(response: Response, db: Session = Depends(get_db)):
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {
            "ready": False,
            "database": f"unhealthy: {exc}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    return {
        "ready": True,
        "database": "healthy",
        "cache_backend": cache_manager.stats().get("backend"),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
