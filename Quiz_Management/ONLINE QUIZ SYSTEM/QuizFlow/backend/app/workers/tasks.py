"""
Asynchronous Background Tasks for Non-Blocking Operations:
- Large quiz CSV report exports
- Platform analytics cache warm-up
- Safe task status tracking (pending -> running -> completed | failed)
Never reports a task completed before execution actually finishes.
Critical quiz answer saving & submission remain strictly transactional on the request path.
"""
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.core.cache import cache_manager
from app.core.monitoring import metrics_collector
from app.database.session import SessionLocal
from app.services.admin_service import AdminService
from app.services.quiz_service import QuizService
from app.workers.celery_app import celery_app

logger = logging.getLogger("quizflow.tasks")

# Bounded local thread pool fallback when Celery/Redis worker is not configured
_local_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qf-bg-")
_local_jobs_lock = threading.Lock()
_local_jobs: Dict[str, Dict[str, Any]] = {}
_MAX_LOCAL_JOBS = 200


def _save_job_state(task_id: str, state: Dict[str, Any], ttl: int = 1800) -> None:
    redis_client = cache_manager.get_redis_client()
    if redis_client is not None:
        try:
            redis_client.setex(f"qf:task:{task_id}", ttl, json.dumps(state, default=str))
            return
        except Exception:
            pass
    with _local_jobs_lock:
        _local_jobs[task_id] = state
        if len(_local_jobs) > _MAX_LOCAL_JOBS:
            oldest_key = next(iter(_local_jobs))
            _local_jobs.pop(oldest_key, None)


def get_job_state(task_id: str, owner_user_id: Optional[int] = None, is_admin: bool = False) -> Optional[Dict[str, Any]]:
    state: Optional[Dict[str, Any]] = None
    redis_client = cache_manager.get_redis_client()
    if redis_client is not None:
        try:
            raw = redis_client.get(f"qf:task:{task_id}")
            if raw:
                state = json.loads(raw)
        except Exception:
            pass
    if state is None:
        with _local_jobs_lock:
            state = _local_jobs.get(task_id)

    if state is None:
        return None
    if not is_admin and owner_user_id is not None and state.get("owner_user_id") != owner_user_id:
        return None
    return state


def _run_csv_export(task_id: str, quiz_id: int, user_id: int, is_admin: bool) -> None:
    _save_job_state(
        task_id,
        {
            "task_id": task_id,
            "task_type": "quiz_csv_export",
            "quiz_id": quiz_id,
            "owner_user_id": user_id,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db = SessionLocal()
    try:
        csv_content = QuizService.export_quiz_csv(db, quiz_id=quiz_id, user_id=user_id, is_admin=is_admin)
        _save_job_state(
            task_id,
            {
                "task_id": task_id,
                "task_type": "quiz_csv_export",
                "quiz_id": quiz_id,
                "owner_user_id": user_id,
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "row_count": max(0, csv_content.count("\n") - 1),
                "content": csv_content,
            },
        )
        metrics_collector.record_bg_task("succeeded")
    except Exception as exc:
        logger.exception("Background CSV export task %s failed: %s", task_id, exc)
        _save_job_state(
            task_id,
            {
                "task_id": task_id,
                "task_type": "quiz_csv_export",
                "quiz_id": quiz_id,
                "owner_user_id": user_id,
                "status": "failed",
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            },
        )
        metrics_collector.record_bg_task("failed")
    finally:
        db.close()


def _run_refresh_analytics(task_id: str, user_id: int) -> None:
    _save_job_state(
        task_id,
        {
            "task_id": task_id,
            "task_type": "refresh_platform_analytics",
            "owner_user_id": user_id,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    db = SessionLocal()
    try:
        cache_manager.invalidate_attempts_and_stats()
        stats = AdminService.get_dashboard_stats(db)
        AdminService.get_platform_reports(db)
        _save_job_state(
            task_id,
            {
                "task_id": task_id,
                "task_type": "refresh_platform_analytics",
                "owner_user_id": user_id,
                "status": "completed",
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "summary": {
                    "total_users": stats.total_users,
                    "total_quizzes": stats.total_quizzes,
                    "total_attempts": stats.total_attempts,
                },
            },
        )
        metrics_collector.record_bg_task("succeeded")
    except Exception as exc:
        logger.exception("Background analytics refresh task %s failed: %s", task_id, exc)
        _save_job_state(
            task_id,
            {
                "task_id": task_id,
                "task_type": "refresh_platform_analytics",
                "owner_user_id": user_id,
                "status": "failed",
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            },
        )
        metrics_collector.record_bg_task("failed")
    finally:
        db.close()


if celery_app is not None:
    @celery_app.task(
        name="quizflow.generate_quiz_csv_export",
        bind=True,
        autoretry_for=(Exception,),
        retry_backoff=True,
        retry_kwargs={"max_retries": 3},
    )
    def generate_quiz_csv_export_celery(self, task_id: str, quiz_id: int, user_id: int, is_admin: bool) -> dict:
        _run_csv_export(task_id, quiz_id, user_id, is_admin)
        return {"task_id": task_id, "status": "done"}

    @celery_app.task(
        name="quizflow.refresh_platform_analytics",
        bind=True,
        autoretry_for=(Exception,),
        retry_backoff=True,
        retry_kwargs={"max_retries": 3},
    )
    def refresh_platform_analytics_celery(self, task_id: str, user_id: int) -> dict:
        _run_refresh_analytics(task_id, user_id)
        return {"task_id": task_id, "status": "done"}


def enqueue_csv_export(quiz_id: int, user_id: int, is_admin: bool) -> Dict[str, Any]:
    """Queue a non-blocking CSV export job via Celery (if Redis broker is active) or bounded local executor."""
    task_id = uuid.uuid4().hex
    initial = {
        "task_id": task_id,
        "task_type": "quiz_csv_export",
        "quiz_id": quiz_id,
        "owner_user_id": user_id,
        "status": "pending",
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_job_state(task_id, initial)
    metrics_collector.record_bg_task("submitted")

    if celery_app is not None and cache_manager.get_redis_client() is not None:
        try:
            generate_quiz_csv_export_celery.delay(task_id, quiz_id, user_id, is_admin)
            return initial
        except Exception as exc:
            logger.warning("Celery enqueue failed (%s); falling back to local background executor.", exc)

    _local_executor.submit(_run_csv_export, task_id, quiz_id, user_id, is_admin)
    return initial


def enqueue_analytics_refresh(user_id: int) -> Dict[str, Any]:
    """Queue a non-blocking platform analytics refresh job."""
    task_id = uuid.uuid4().hex
    initial = {
        "task_id": task_id,
        "task_type": "refresh_platform_analytics",
        "owner_user_id": user_id,
        "status": "pending",
        "queued_at": datetime.now(timezone.utc).isoformat(),
    }
    _save_job_state(task_id, initial)
    metrics_collector.record_bg_task("submitted")

    if celery_app is not None and cache_manager.get_redis_client() is not None:
        try:
            refresh_platform_analytics_celery.delay(task_id, user_id)
            return initial
        except Exception as exc:
            logger.warning("Celery enqueue failed (%s); falling back to local background executor.", exc)

    _local_executor.submit(_run_refresh_analytics, task_id, user_id)
    return initial
