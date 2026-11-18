"""
Admin Statistics and Platform Management Service.
Eliminates N+1 queries with single-pass SQL conditional aggregates, JOINs,
server-side pagination (offset & keyset cursor), and Redis/LRU caching with invalidation.
"""
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.cache import cache_manager
from app.core.config import settings
from app.database.session import execute_with_retry
from app.models.attempt import AttemptStatus, QuizAttempt
from app.models.audit_log import AuditLog
from app.models.quiz import Quiz
from app.models.user import User, UserRole
from app.schemas.admin import (
    DashboardStatsResponse,
    PlatformReportsResponse,
    RecentActivityItem,
    ResultListItem,
    UserManagementItem,
)
from app.schemas.user import AuditLogResponse
from app.services.audit_service import AuditService

FINISHED_STATUSES = (AttemptStatus.COMPLETED.value, AttemptStatus.EXPIRED.value)
PARTICIPANT_ROLES = (UserRole.PARTICIPANT.value, UserRole.STUDENT.value)


def _clamp_limit(limit: Optional[int], default: Optional[int] = None) -> int:
    fallback = default if default is not None else settings.DEFAULT_PAGE_SIZE
    if limit is None:
        return fallback
    return max(1, min(limit, settings.MAX_PAGE_SIZE))


class AdminService:
    @staticmethod
    def get_dashboard_stats(db: Session) -> DashboardStatsResponse:
        cache_key = "admin:dashboard:stats"
        cached = cache_manager.get(cache_key)
        if cached is not None:
            return DashboardStatsResponse.model_validate(cached)

        # Query 1: User role counts in a single table scan/index pass
        user_agg = db.query(
            func.count(User.id).label("total_users"),
            func.coalesce(func.sum(case((User.role.in_(PARTICIPANT_ROLES), 1), else_=0)), 0).label("participants"),
            func.coalesce(func.sum(case((User.role == UserRole.ORGANIZER.value, 1), else_=0)), 0).label("organizers"),
        ).one()

        total_users = int(user_agg.total_users or 0)
        total_participants = int(user_agg.participants or 0)
        total_organizers = int(user_agg.organizers or 0)

        # Query 2: Quiz status counts in a single pass
        quiz_agg = db.query(
            func.count(Quiz.id).label("total_quizzes"),
            func.coalesce(
                func.sum(case(((Quiz.is_published == True) & (Quiz.is_disabled == False), 1), else_=0)), 0  # noqa: E712
            ).label("published"),
            func.coalesce(func.sum(case((Quiz.is_published == False, 1), else_=0)), 0).label("draft"),  # noqa: E712
            func.coalesce(func.sum(case((Quiz.is_disabled == True, 1), else_=0)), 0).label("disabled"),  # noqa: E712
        ).one()

        total_quizzes = int(quiz_agg.total_quizzes or 0)
        published_quizzes = int(quiz_agg.published or 0)
        draft_quizzes = int(quiz_agg.draft or 0)
        disabled_quizzes = int(quiz_agg.disabled or 0)

        # Query 3: Attempt totals, pass counts, and average score computed purely in SQL
        attempt_agg = db.query(
            func.count(QuizAttempt.id).label("total_attempts"),
            func.coalesce(func.sum(case((QuizAttempt.status.in_(FINISHED_STATUSES), 1), else_=0)), 0).label("completed"),
            func.coalesce(
                func.sum(
                    case(((QuizAttempt.status.in_(FINISHED_STATUSES)) & (QuizAttempt.is_passed == True), 1), else_=0)  # noqa: E712
                ),
                0,
            ).label("passed"),
            func.avg(case((QuizAttempt.status.in_(FINISHED_STATUSES), QuizAttempt.percentage), else_=None)).label(
                "avg_score"
            ),
        ).one()

        total_attempts = int(attempt_agg.total_attempts or 0)
        completed_attempts = int(attempt_agg.completed or 0)
        passed_attempts = int(attempt_agg.passed or 0)
        average_score = round(float(attempt_agg.avg_score), 1) if attempt_agg.avg_score is not None else 0.0
        average_pass_rate = (
            round((passed_attempts / completed_attempts) * 100.0, 1) if completed_attempts > 0 else 0.0
        )

        # Query 4: Recent 8 attempts with JOINed student name & quiz title (ZERO N+1!)
        recent_rows = (
            db.query(
                QuizAttempt.id,
                QuizAttempt.score_obtained,
                QuizAttempt.total_marks,
                QuizAttempt.percentage,
                QuizAttempt.is_passed,
                QuizAttempt.status,
                QuizAttempt.end_time,
                QuizAttempt.start_time,
                User.full_name.label("student_name"),
                Quiz.title.label("quiz_title"),
            )
            .outerjoin(User, User.id == QuizAttempt.student_id)
            .outerjoin(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
            .limit(8)
            .all()
        )

        recent_items: List[RecentActivityItem] = [
            RecentActivityItem(
                attempt_id=r.id,
                student_name=r.student_name or "Unknown",
                quiz_title=r.quiz_title or "Quiz",
                score_obtained=r.score_obtained,
                total_marks=r.total_marks,
                percentage=r.percentage,
                is_passed=r.is_passed,
                status=r.status,
                submitted_at=r.end_time or r.start_time,
            )
            for r in recent_rows
        ]

        # Query 5: Recent 5 users (selective columns)
        recent_users_rows = (
            db.query(User.id, User.full_name, User.email, User.role, User.created_at)
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(5)
            .all()
        )
        recent_users_list = [
            {
                "id": u.id,
                "full_name": u.full_name,
                "email": u.email,
                "role": u.role,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in recent_users_rows
        ]

        # Queries 6 & 7: 7-day trend grouped in SQL instead of 14 separate COUNT queries
        now = datetime.now(timezone.utc)
        start_window = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)

        attempt_day_rows = (
            db.query(func.date(QuizAttempt.created_at).label("day"), func.count(QuizAttempt.id))
            .filter(QuizAttempt.created_at >= start_window)
            .group_by(func.date(QuizAttempt.created_at))
            .all()
        )
        attempts_by_day = {str(day): int(cnt) for day, cnt in attempt_day_rows if day is not None}

        quiz_day_rows = (
            db.query(func.date(Quiz.created_at).label("day"), func.count(Quiz.id))
            .filter(Quiz.created_at >= start_window)
            .group_by(func.date(Quiz.created_at))
            .all()
        )
        quizzes_by_day = {str(day): int(cnt) for day, cnt in quiz_day_rows if day is not None}

        activity_overview = []
        for i in range(6, -1, -1):
            day = now - timedelta(days=i)
            iso_key = day.strftime("%Y-%m-%d")
            activity_overview.append(
                {
                    "date": day.strftime("%b %d"),
                    "attempts": attempts_by_day.get(iso_key, 0),
                    "quizzes": quizzes_by_day.get(iso_key, 0),
                }
            )

        response = DashboardStatsResponse(
            total_users=total_users,
            total_students=total_participants,
            total_participants=total_participants,
            total_organizers=total_organizers,
            total_quizzes=total_quizzes,
            published_quizzes=published_quizzes,
            draft_quizzes=draft_quizzes,
            disabled_quizzes=disabled_quizzes,
            total_attempts=total_attempts,
            completed_attempts=completed_attempts,
            average_score=average_score,
            average_pass_rate=average_pass_rate,
            recent_activity=recent_items,
            recent_users=recent_users_list,
            activity_overview=activity_overview,
        )
        cache_manager.set(cache_key, response.model_dump(mode="json"), ttl=settings.CACHE_DASHBOARD_TTL)
        return response

    @staticmethod
    def get_users_list(
        db: Session,
        role: Optional[str] = None,
        search: Optional[str] = None,
        is_active: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        return_total: bool = False,
    ) -> List[UserManagementItem] | Tuple[List[UserManagementItem], int]:
        page_limit = _clamp_limit(limit)
        page_offset = max(0, offset)

        # Grouped subqueries eliminate 3N+1 queries completely
        quiz_counts_sq = (
            db.query(
                Quiz.created_by_id.label("uid"),
                func.count(Quiz.id).label("quizzes_created"),
            )
            .group_by(Quiz.created_by_id)
            .subquery()
        )
        attempt_stats_sq = (
            db.query(
                QuizAttempt.student_id.label("uid"),
                func.count(QuizAttempt.id).label("attempts_count"),
                func.avg(
                    case((QuizAttempt.status.in_(FINISHED_STATUSES), QuizAttempt.percentage), else_=None)
                ).label("avg_percentage"),
            )
            .group_by(QuizAttempt.student_id)
            .subquery()
        )

        base_filter = []
        if role:
            clean_role = role.strip().lower()
            if clean_role in ("participant", "student"):
                base_filter.append(User.role.in_(PARTICIPANT_ROLES))
            else:
                base_filter.append(User.role == clean_role)

        if is_active is not None:
            base_filter.append(User.is_active == is_active)

        if search:
            s = f"%{search.strip().lower()}%"
            base_filter.append((func.lower(User.email).like(s)) | (func.lower(User.full_name).like(s)))

        query = (
            db.query(
                User.id,
                User.email,
                User.full_name,
                User.role,
                User.is_active,
                User.created_at,
                func.coalesce(quiz_counts_sq.c.quizzes_created, 0).label("quizzes_created"),
                func.coalesce(attempt_stats_sq.c.attempts_count, 0).label("attempts_count"),
                func.coalesce(attempt_stats_sq.c.avg_percentage, 0.0).label("avg_percentage"),
            )
            .outerjoin(quiz_counts_sq, quiz_counts_sq.c.uid == User.id)
            .outerjoin(attempt_stats_sq, attempt_stats_sq.c.uid == User.id)
        )
        if base_filter:
            query = query.filter(*base_filter)

        rows = (
            query.order_by(User.created_at.desc(), User.id.desc())
            .offset(page_offset)
            .limit(page_limit)
            .all()
        )

        items = [
            UserManagementItem(
                id=r.id,
                email=r.email,
                full_name=r.full_name,
                role="participant" if r.role == "student" else r.role,
                is_active=r.is_active,
                created_at=r.created_at,
                quizzes_created=int(r.quizzes_created or 0),
                attempts_count=int(r.attempts_count or 0),
                avg_percentage=round(float(r.avg_percentage or 0.0), 1),
            )
            for r in rows
        ]

        if return_total:
            cnt_q = db.query(func.count(User.id))
            if base_filter:
                cnt_q = cnt_q.filter(*base_filter)
            total = int(cnt_q.scalar() or 0)
            return items, total
        return items

    @staticmethod
    def get_students_list(
        db: Session,
        limit: Optional[int] = None,
        offset: int = 0,
        return_total: bool = False,
    ) -> List[UserManagementItem] | Tuple[List[UserManagementItem], int]:
        return AdminService.get_users_list(
            db, role="participant", limit=limit, offset=offset, return_total=return_total
        )

    @staticmethod
    def set_user_status(
        db: Session,
        user_id: int,
        is_active: bool,
        admin_id: int,
        reason: Optional[str] = None,
    ) -> UserManagementItem:
        user = db.get(User, user_id)
        if not user:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

        if user.id == admin_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Administrators cannot suspend their own account.",
            )

        if user.role == UserRole.ADMIN.value and not is_active:
            other_admins = (
                db.query(func.count(User.id))
                .filter(User.role == UserRole.ADMIN.value, User.id != user_id, User.is_active == True)  # noqa: E712
                .scalar()
                or 0
            )
            if other_admins == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Cannot deactivate the sole remaining active administrator.",
                )

        def _apply() -> None:
            user.is_active = is_active
            action_name = "USER_REACTIVATE" if is_active else "USER_SUSPEND"
            AuditService.log(
                db,
                action=action_name,
                user_id=admin_id,
                resource_type="user",
                resource_id=user.id,
                details={"email": user.email, "is_active": is_active, "reason": reason},
                commit=False,
            )
            db.commit()
            db.refresh(user)

        execute_with_retry(db, _apply)
        cache_manager.invalidate_users()

        quizzes_cnt = int(db.query(func.count(Quiz.id)).filter(Quiz.created_by_id == user.id).scalar() or 0)
        att_row = (
            db.query(
                func.count(QuizAttempt.id),
                func.avg(case((QuizAttempt.status.in_(FINISHED_STATUSES), QuizAttempt.percentage), else_=None)),
            )
            .filter(QuizAttempt.student_id == user.id)
            .one()
        )
        return UserManagementItem(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role="participant" if user.role == "student" else user.role,
            is_active=user.is_active,
            created_at=user.created_at,
            quizzes_created=quizzes_cnt,
            attempts_count=int(att_row[0] or 0),
            avg_percentage=round(float(att_row[1] or 0.0), 1),
        )

    @staticmethod
    def moderate_quiz(
        db: Session,
        quiz_id: int,
        is_disabled: bool,
        admin_id: int,
        reason: Optional[str] = None,
    ) -> bool:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        def _apply() -> None:
            quiz.is_disabled = is_disabled
            action_name = "QUIZ_MODERATE_DISABLE" if is_disabled else "QUIZ_MODERATE_ENABLE"
            AuditService.log(
                db,
                action=action_name,
                user_id=admin_id,
                resource_type="quiz",
                resource_id=quiz.id,
                details={"title": quiz.title, "is_disabled": is_disabled, "reason": reason},
                commit=False,
            )
            db.commit()

        execute_with_retry(db, _apply)
        cache_manager.invalidate_quizzes(quiz_id=quiz_id)
        return True

    @staticmethod
    def get_audit_logs(
        db: Session,
        action: Optional[str] = None,
        user_id: Optional[int] = None,
        limit: int = 50,
        offset: int = 0,
        cursor_id: Optional[int] = None,
        return_total: bool = False,
    ) -> List[AuditLogResponse] | Tuple[List[AuditLogResponse], int]:
        page_limit = _clamp_limit(limit, default=50)
        page_offset = max(0, offset)

        filters = []
        if action:
            filters.append(AuditLog.action == action.upper())
        if user_id:
            filters.append(AuditLog.user_id == user_id)

        query = (
            db.query(
                AuditLog.id,
                AuditLog.user_id,
                User.email.label("user_email"),
                AuditLog.action,
                AuditLog.resource_type,
                AuditLog.resource_id,
                AuditLog.details,
                AuditLog.ip_address,
                AuditLog.created_at,
            )
            .outerjoin(User, User.id == AuditLog.user_id)
        )
        if filters:
            query = query.filter(*filters)

        # Keyset cursor pagination support for large audit tables (consistent id DESC ordering)
        if cursor_id is not None:
            query = query.filter(AuditLog.id < cursor_id)
            rows = query.order_by(AuditLog.id.desc()).limit(page_limit).all()
        else:
            rows = (
                query.order_by(AuditLog.id.desc())
                .offset(page_offset)
                .limit(page_limit)
                .all()
            )

        items = [
            AuditLogResponse(
                id=r.id,
                user_id=r.user_id,
                user_email=r.user_email,
                action=r.action,
                resource_type=r.resource_type,
                resource_id=r.resource_id,
                details=r.details,
                ip_address=r.ip_address,
                created_at=r.created_at,
            )
            for r in rows
        ]

        if return_total:
            cnt_q = db.query(func.count(AuditLog.id))
            if filters:
                cnt_q = cnt_q.filter(*filters)
            total = int(cnt_q.scalar() or 0)
            return items, total
        return items

    @staticmethod
    def get_platform_reports(db: Session) -> PlatformReportsResponse:
        cache_key = "admin:reports:platform"
        cached = cache_manager.get(cache_key)
        if cached is not None:
            return PlatformReportsResponse.model_validate(cached)

        # Query 1: Role distribution
        user_row = db.query(
            func.coalesce(func.sum(case((User.role.in_(PARTICIPANT_ROLES), 1), else_=0)), 0),
            func.coalesce(func.sum(case((User.role == UserRole.ORGANIZER.value, 1), else_=0)), 0),
            func.coalesce(func.sum(case((User.role == UserRole.ADMIN.value, 1), else_=0)), 0),
        ).one()
        role_dist = {
            "participants": int(user_row[0] or 0),
            "organizers": int(user_row[1] or 0),
            "admins": int(user_row[2] or 0),
        }

        # Query 2: Quiz status distribution
        quiz_row = db.query(
            func.coalesce(
                func.sum(case(((Quiz.is_published == True) & (Quiz.is_disabled == False), 1), else_=0)), 0  # noqa: E712
            ),
            func.coalesce(func.sum(case((Quiz.is_published == False, 1), else_=0)), 0),  # noqa: E712
            func.coalesce(func.sum(case((Quiz.is_disabled == True, 1), else_=0)), 0),  # noqa: E712
        ).one()
        quiz_dist = {
            "published": int(quiz_row[0] or 0),
            "draft": int(quiz_row[1] or 0),
            "disabled": int(quiz_row[2] or 0),
        }

        # Query 3: Attempt status distribution + pass rate
        att_row = db.query(
            func.coalesce(func.sum(case((QuizAttempt.status == AttemptStatus.COMPLETED.value, 1), else_=0)), 0),
            func.coalesce(func.sum(case((QuizAttempt.status == AttemptStatus.IN_PROGRESS.value, 1), else_=0)), 0),
            func.coalesce(func.sum(case((QuizAttempt.status == AttemptStatus.EXPIRED.value, 1), else_=0)), 0),
            func.coalesce(
                func.sum(
                    case(((QuizAttempt.status.in_(FINISHED_STATUSES)) & (QuizAttempt.is_passed == True), 1), else_=0)  # noqa: E712
                ),
                0,
            ),
        ).one()

        completed_cnt = int(att_row[0] or 0)
        in_prog_cnt = int(att_row[1] or 0)
        expired_cnt = int(att_row[2] or 0)
        passed_cnt = int(att_row[3] or 0)

        attempt_dist = {
            "completed": completed_cnt,
            "in_progress": in_prog_cnt,
            "expired": expired_cnt,
        }
        tot_finished = completed_cnt + expired_cnt
        pass_rate = round((passed_cnt / tot_finished) * 100.0, 1) if tot_finished > 0 else 0.0

        resp = PlatformReportsResponse(
            role_distribution=role_dist,
            quiz_status_distribution=quiz_dist,
            attempt_status_distribution=attempt_dist,
            pass_rate=pass_rate,
        )
        cache_manager.set(cache_key, resp.model_dump(mode="json"), ttl=settings.CACHE_DASHBOARD_TTL)
        return resp

    @staticmethod
    def get_all_results(
        db: Session,
        quiz_id: Optional[int] = None,
        status_filter: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        cursor_id: Optional[int] = None,
        return_total: bool = False,
    ) -> List[ResultListItem] | Tuple[List[ResultListItem], int]:
        page_limit = _clamp_limit(limit)
        page_offset = max(0, offset)

        filters = []
        if quiz_id is not None:
            filters.append(QuizAttempt.quiz_id == quiz_id)
        if status_filter:
            filters.append(QuizAttempt.status == status_filter.strip().lower())

        query = (
            db.query(
                QuizAttempt.id,
                QuizAttempt.student_id,
                User.full_name.label("student_name"),
                User.email.label("student_email"),
                QuizAttempt.quiz_id,
                Quiz.title.label("quiz_title"),
                QuizAttempt.status,
                QuizAttempt.score_obtained,
                QuizAttempt.total_marks,
                QuizAttempt.percentage,
                QuizAttempt.is_passed,
                QuizAttempt.start_time,
                QuizAttempt.end_time,
            )
            .outerjoin(User, User.id == QuizAttempt.student_id)
            .outerjoin(Quiz, Quiz.id == QuizAttempt.quiz_id)
        )
        if filters:
            query = query.filter(*filters)

        if cursor_id is not None:
            query = query.filter(QuizAttempt.id < cursor_id)
            rows = query.order_by(QuizAttempt.id.desc()).limit(page_limit).all()
        else:
            rows = (
                query.order_by(QuizAttempt.id.desc())
                .offset(page_offset)
                .limit(page_limit)
                .all()
            )

        items = [
            ResultListItem(
                attempt_id=r.id,
                student_id=r.student_id,
                student_name=r.student_name or "Unknown",
                student_email=r.student_email or "N/A",
                quiz_id=r.quiz_id,
                quiz_title=r.quiz_title or "Quiz",
                status=r.status,
                score_obtained=r.score_obtained,
                total_marks=r.total_marks,
                percentage=r.percentage,
                is_passed=r.is_passed,
                start_time=r.start_time,
                end_time=r.end_time,
            )
            for r in rows
        ]

        if return_total:
            cnt_q = db.query(func.count(QuizAttempt.id))
            if filters:
                cnt_q = cnt_q.filter(*filters)
            total = int(cnt_q.scalar() or 0)
            return items, total
        return items
