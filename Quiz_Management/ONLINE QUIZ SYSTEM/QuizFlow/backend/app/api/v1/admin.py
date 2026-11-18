"""
Admin Statistics, User Management, Quiz Moderation, Audit Logs, and Performance Metrics Endpoints.
All list endpoints enforce server-side pagination and emit X-Total-Count / X-Has-More headers.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_admin, get_db
from app.core.cache import cache_manager
from app.core.config import settings
from app.core.monitoring import metrics_collector
from app.database.session import get_pool_stats
from app.models.user import User
from app.schemas.admin import (
    DashboardStatsResponse,
    PlatformReportsResponse,
    QuizModerationRequest,
    ResultListItem,
    UserManagementItem,
)
from app.schemas.user import AuditLogResponse, UserStatusUpdate
from app.services.admin_service import AdminService
from app.workers.tasks import enqueue_analytics_refresh, get_job_state

router = APIRouter()


def _set_pagination_headers(
    response: Response, total: int, limit: int, offset: int, returned_count: int, next_cursor: Optional[int] = None
) -> None:
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["X-Has-More"] = "true" if (offset + returned_count) < total else "false"
    if next_cursor is not None:
        response.headers["X-Next-Cursor"] = str(next_cursor)


@router.get(
    "/dashboard-stats",
    response_model=DashboardStatsResponse,
    summary="Get aggregated statistics for admin dashboard",
)
def get_dashboard_stats(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    return AdminService.get_dashboard_stats(db)


@router.get(
    "/users",
    response_model=List[UserManagementItem],
    summary="List and search platform users with server-side pagination (Admin only)",
)
def list_users(
    response: Response,
    role: Optional[str] = Query(None, description="Filter by role: admin, organizer, participant"),
    search: Optional[str] = Query(None, description="Search by name or email"),
    is_active: Optional[bool] = Query(None, description="Filter active/suspended accounts"),
    limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    items, total = AdminService.get_users_list(
        db, role=role, search=search, is_active=is_active, limit=limit, offset=offset, return_total=True
    )
    _set_pagination_headers(response, total, limit, offset, len(items))
    return items


@router.get(
    "/students",
    response_model=List[UserManagementItem],
    summary="List registered students/participants with server-side pagination (Admin only)",
)
def list_students(
    response: Response,
    limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    items, total = AdminService.get_students_list(db, limit=limit, offset=offset, return_total=True)
    _set_pagination_headers(response, total, limit, offset, len(items))
    return items


@router.post(
    "/users/{user_id}/status",
    response_model=UserManagementItem,
    summary="Suspend or reactivate a user account (Admin only)",
)
@router.patch(
    "/users/{user_id}/status",
    response_model=UserManagementItem,
    include_in_schema=False,
)
def set_user_status(
    user_id: int,
    payload: UserStatusUpdate,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    return AdminService.set_user_status(
        db,
        user_id=user_id,
        is_active=payload.is_active,
        admin_id=current_admin.id,
        reason=payload.reason,
    )


@router.post(
    "/quizzes/{quiz_id}/moderate",
    summary="Moderate a quiz: disable or re-enable (Admin only)",
)
@router.patch(
    "/quizzes/{quiz_id}/moderate",
    include_in_schema=False,
)
def moderate_quiz(
    quiz_id: int,
    payload: QuizModerationRequest,
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    AdminService.moderate_quiz(
        db,
        quiz_id=quiz_id,
        is_disabled=payload.is_disabled,
        admin_id=current_admin.id,
        reason=payload.reason,
    )
    action = "disabled" if payload.is_disabled else "re-enabled"
    return {"message": f"Quiz {action} successfully.", "quiz_id": quiz_id, "is_disabled": payload.is_disabled}


@router.get(
    "/audit-logs",
    response_model=List[AuditLogResponse],
    summary="List platform security and activity audit logs with offset or cursor pagination (Admin only)",
)
def list_audit_logs(
    response: Response,
    action: Optional[str] = Query(None, description="Filter by action code"),
    user_id: Optional[int] = Query(None, description="Filter by actor user ID"),
    limit: int = Query(50, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    cursor_id: Optional[int] = Query(None, ge=1, description="Keyset cursor (return logs with id < cursor_id)"),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    items, total = AdminService.get_audit_logs(
        db, action=action, user_id=user_id, limit=limit, offset=offset, cursor_id=cursor_id, return_total=True
    )
    next_cursor = items[-1].id if items else None
    _set_pagination_headers(response, total, limit, offset, len(items), next_cursor=next_cursor)
    return items


@router.get(
    "/platform-reports",
    response_model=PlatformReportsResponse,
    summary="Get platform-wide distribution reports and trends (Admin only)",
)
def get_platform_reports(
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    return AdminService.get_platform_reports(db)


@router.get(
    "/results",
    response_model=List[ResultListItem],
    summary="List quiz attempt results across students with server-side pagination (Admin only)",
)
def list_all_results(
    response: Response,
    quiz_id: Optional[int] = Query(None, ge=1),
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    cursor_id: Optional[int] = Query(None, ge=1, description="Keyset cursor (return attempts with id < cursor_id)"),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    items, total = AdminService.get_all_results(
        db,
        quiz_id=quiz_id,
        status_filter=status_filter,
        limit=limit,
        offset=offset,
        cursor_id=cursor_id,
        return_total=True,
    )
    next_cursor = items[-1].attempt_id if items else None
    _set_pagination_headers(response, total, limit, offset, len(items), next_cursor=next_cursor)
    return items


@router.get(
    "/metrics",
    summary="Get real-time performance monitoring metrics (Admin only)",
)
def get_performance_metrics(
    current_admin: User = Depends(get_current_admin),
):
    return metrics_collector.snapshot(
        db_pool_stats=get_pool_stats(),
        cache_stats=cache_manager.stats(),
    )


@router.get(
    "/metrics/prometheus",
    summary="Export performance metrics in Prometheus text exposition format (Admin only)",
)
def get_prometheus_metrics(
    current_admin: User = Depends(get_current_admin),
):
    body = metrics_collector.prometheus_text(
        db_pool_stats=get_pool_stats(),
        cache_stats=cache_manager.stats(),
    )
    return Response(content=body, media_type="text/plain; version=0.0.4")


@router.post(
    "/analytics/refresh",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger asynchronous refresh of platform analytics cache (Admin only)",
)
def trigger_analytics_refresh(
    current_admin: User = Depends(get_current_admin),
):
    return enqueue_analytics_refresh(user_id=current_admin.id)


@router.get(
    "/tasks/{task_id}",
    summary="Check status of an admin background task",
)
def get_admin_task_status(
    task_id: str,
    current_admin: User = Depends(get_current_admin),
):
    state = get_job_state(task_id, owner_user_id=current_admin.id, is_admin=True)
    if not state:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return state
