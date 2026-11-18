"""
Celery Application Configuration for QuizFlow Background Task Processing.
Uses Redis as broker and result backend when configured (CELERY_BROKER_URL / REDIS_URL).
Includes safe serialization (JSON only), retries, soft/hard time limits, and late acknowledgments.
"""
import logging

from app.core.config import settings

logger = logging.getLogger("quizflow.celery")

celery_app = None

if settings.CELERY_BROKER_URL:
    try:
        from celery import Celery

        celery_app = Celery(
            "quizflow",
            broker=settings.CELERY_BROKER_URL,
            backend=settings.CELERY_RESULT_BACKEND or settings.CELERY_BROKER_URL,
            include=["app.workers.tasks"],
        )
        celery_app.conf.update(
            task_serializer="json",
            accept_content=["json"],
            result_serializer="json",
            timezone="UTC",
            enable_utc=True,
            task_acks_late=True,
            task_reject_on_worker_lost=True,
            worker_prefetch_multiplier=1,
            task_soft_time_limit=120,
            task_time_limit=180,
            result_expires=3600,
            broker_connection_retry_on_startup=True,
        )
    except Exception as exc:
        logger.warning("Celery initialization skipped (%s); local background execution will be used.", exc)
        celery_app = None
