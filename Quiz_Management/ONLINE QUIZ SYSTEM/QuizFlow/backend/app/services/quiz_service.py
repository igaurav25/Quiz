"""
Quiz and Question Management Service with Multi-Role Ownership, Join Codes,
Zero-N+1 SQL Aggregations, Server-Side Pagination, and Cache Invalidation.
"""
import csv
import io
import random
import string
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.cache import cache_manager
from app.core.config import settings
from app.database.session import execute_with_retry
from app.models.attempt import AttemptStatus, QuizAttempt
from app.models.attempt_answer import AttemptAnswer
from app.models.question import Question
from app.models.quiz import Quiz
from app.models.user import User
from app.schemas.question import QuestionAdminResponse, QuestionCreate, QuestionUpdate
from app.schemas.quiz import (
    OrganizerDashboardStats,
    ParticipantAttemptItem,
    QuestionPerformanceStat,
    QuizAdminResponse,
    QuizCreate,
    QuizDetailStudentResponse,
    QuizReportResponse,
    QuizStudentResponse,
    QuizUpdate,
)
from app.services.audit_service import AuditService

FINISHED_STATUSES = (AttemptStatus.COMPLETED.value, AttemptStatus.EXPIRED.value)


def _generate_code() -> str:
    chars = "".join(c for c in string.ascii_uppercase + string.digits if c not in "IO01")
    return "QF-" + "".join(random.choices(chars, k=4))


def _clamp_limit(limit: Optional[int], default: Optional[int] = None) -> int:
    fallback = default if default is not None else settings.DEFAULT_PAGE_SIZE
    if limit is None:
        return fallback
    return max(1, min(limit, settings.MAX_PAGE_SIZE))


class QuizService:
    @staticmethod
    def _get_unique_join_code(db: Session) -> str:
        for _ in range(10):
            code = _generate_code()
            if not db.query(Quiz.id).filter(Quiz.join_code == code).first():
                return code
        return "QF-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6))

    @staticmethod
    def create_quiz(db: Session, quiz_in: QuizCreate, creator_id: int) -> QuizAdminResponse:
        def _create() -> Quiz:
            join_code = QuizService._get_unique_join_code(db)
            quiz = Quiz(
                title=quiz_in.title.strip(),
                subject=(quiz_in.subject or "General").strip(),
                description=quiz_in.description.strip() if quiz_in.description else None,
                instructions=quiz_in.instructions.strip() if quiz_in.instructions else None,
                duration_minutes=quiz_in.duration_minutes,
                passing_percentage=quiz_in.passing_percentage,
                total_marks=0,
                is_published=False,
                is_public=quiz_in.is_public,
                max_attempts=quiz_in.max_attempts,
                release_results_immediately=quiz_in.release_results_immediately,
                results_released=quiz_in.release_results_immediately,
                start_time=quiz_in.start_time,
                end_time=quiz_in.end_time,
                is_disabled=False,
                join_code=join_code,
                created_by_id=creator_id,
            )
            db.add(quiz)
            db.flush()

            AuditService.log(
                db,
                action="QUIZ_CREATE",
                user_id=creator_id,
                resource_type="quiz",
                resource_id=quiz.id,
                details={"title": quiz.title, "join_code": quiz.join_code, "is_public": quiz.is_public},
                commit=False,
            )
            db.commit()
            db.refresh(quiz)
            return quiz

        quiz = execute_with_retry(db, _create)
        cache_manager.invalidate_quizzes(quiz_id=quiz.id)

        creator_name = db.query(User.full_name).filter(User.id == creator_id).scalar()
        return QuizService._format_admin_quiz(quiz, question_count=0, creator_name=creator_name)

    @staticmethod
    def update_quiz(
        db: Session,
        quiz_id: int,
        quiz_in: QuizUpdate,
        user_id: int,
        is_admin: bool = False,
    ) -> QuizAdminResponse:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to modify another organizer's quiz.",
            )

        if quiz_in.title is not None:
            quiz.title = quiz_in.title.strip()
        if quiz_in.subject is not None:
            quiz.subject = quiz_in.subject.strip() or "General"
        if quiz_in.description is not None:
            quiz.description = quiz_in.description.strip() if quiz_in.description else None
        if quiz_in.instructions is not None:
            quiz.instructions = quiz_in.instructions.strip() if quiz_in.instructions else None
        if quiz_in.duration_minutes is not None:
            quiz.duration_minutes = quiz_in.duration_minutes
        if quiz_in.passing_percentage is not None:
            quiz.passing_percentage = quiz_in.passing_percentage
        if quiz_in.is_public is not None:
            quiz.is_public = quiz_in.is_public
        if quiz_in.max_attempts is not None:
            quiz.max_attempts = quiz_in.max_attempts
        if quiz_in.release_results_immediately is not None:
            quiz.release_results_immediately = quiz_in.release_results_immediately
        if quiz_in.results_released is not None:
            quiz.results_released = quiz_in.results_released
        if quiz_in.start_time is not None:
            quiz.start_time = quiz_in.start_time
        if quiz_in.end_time is not None:
            quiz.end_time = quiz_in.end_time
        if quiz_in.is_published is not None:
            if quiz_in.is_published:
                QuizService._validate_for_publish(db, quiz)
            quiz.is_published = quiz_in.is_published

        def _commit() -> None:
            AuditService.log(
                db,
                action="QUIZ_UPDATE",
                user_id=user_id,
                resource_type="quiz",
                resource_id=quiz.id,
                details={"title": quiz.title, "is_published": quiz.is_published},
                commit=False,
            )
            db.commit()
            db.refresh(quiz)

        execute_with_retry(db, _commit)
        cache_manager.invalidate_quizzes(quiz_id=quiz.id)

        return QuizService._load_single_admin_quiz(db, quiz)

    @staticmethod
    def delete_quiz(db: Session, quiz_id: int, user_id: int, is_admin: bool = False) -> bool:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delete another organizer's quiz.",
            )

        title = quiz.title

        def _delete() -> None:
            db.delete(quiz)
            AuditService.log(
                db,
                action="QUIZ_DELETE",
                user_id=user_id,
                resource_type="quiz",
                resource_id=quiz_id,
                details={"title": title},
                commit=False,
            )
            db.commit()

        execute_with_retry(db, _delete)
        cache_manager.invalidate_quizzes(quiz_id=quiz_id)
        return True

    @staticmethod
    def publish_quiz(db: Session, quiz_id: int, user_id: int, is_admin: bool = False) -> QuizAdminResponse:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to publish another organizer's quiz.",
            )

        q_count = QuizService._validate_for_publish(db, quiz)
        quiz.is_published = True

        def _commit() -> None:
            AuditService.log(
                db,
                action="QUIZ_PUBLISH",
                user_id=user_id,
                resource_type="quiz",
                resource_id=quiz.id,
                details={"join_code": quiz.join_code},
                commit=False,
            )
            db.commit()
            db.refresh(quiz)

        execute_with_retry(db, _commit)
        cache_manager.invalidate_quizzes(quiz_id=quiz.id)

        creator_name = db.query(User.full_name).filter(User.id == quiz.created_by_id).scalar()
        return QuizService._format_admin_quiz(quiz, question_count=q_count, creator_name=creator_name)

    @staticmethod
    def unpublish_quiz(db: Session, quiz_id: int, user_id: int, is_admin: bool = False) -> QuizAdminResponse:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to unpublish another organizer's quiz.",
            )

        quiz.is_published = False

        def _commit() -> None:
            AuditService.log(
                db,
                action="QUIZ_UNPUBLISH",
                user_id=user_id,
                resource_type="quiz",
                resource_id=quiz.id,
                commit=False,
            )
            db.commit()
            db.refresh(quiz)

        execute_with_retry(db, _commit)
        cache_manager.invalidate_quizzes(quiz_id=quiz.id)

        return QuizService._load_single_admin_quiz(db, quiz)

    @staticmethod
    def release_results(db: Session, quiz_id: int, user_id: int, is_admin: bool = False) -> QuizAdminResponse:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to release results for this quiz.",
            )

        quiz.results_released = True

        def _commit() -> None:
            AuditService.log(
                db,
                action="QUIZ_RELEASE_RESULTS",
                user_id=user_id,
                resource_type="quiz",
                resource_id=quiz.id,
                commit=False,
            )
            db.commit()
            db.refresh(quiz)

        execute_with_retry(db, _commit)
        cache_manager.invalidate_quizzes(quiz_id=quiz.id)

        return QuizService._load_single_admin_quiz(db, quiz)

    @staticmethod
    def _validate_for_publish(db: Session, quiz: Quiz) -> int:
        """Ensure quiz has at least one valid question and positive total marks using SQL aggregate."""
        q_count, total_marks = (
            db.query(func.count(Question.id), func.coalesce(func.sum(Question.marks), 0))
            .filter(Question.quiz_id == quiz.id)
            .one()
        )
        q_count = int(q_count or 0)
        total_marks = int(total_marks or 0)
        if q_count == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot publish a quiz with no questions. Please add questions first.",
            )
        if total_marks <= 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Total marks for the quiz must be greater than zero.",
            )
        quiz.total_marks = total_marks
        return q_count

    @staticmethod
    def _load_single_admin_quiz(db: Session, quiz: Quiz) -> QuizAdminResponse:
        q_count = int(db.query(func.count(Question.id)).filter(Question.quiz_id == quiz.id).scalar() or 0)
        a_count = int(db.query(func.count(QuizAttempt.id)).filter(QuizAttempt.quiz_id == quiz.id).scalar() or 0)
        creator_name = db.query(User.full_name).filter(User.id == quiz.created_by_id).scalar()
        return QuizService._format_admin_quiz(
            quiz, question_count=q_count, attempt_count=a_count, creator_name=creator_name
        )

    # ------------------ Questions Management with Ownership ------------------
    @staticmethod
    def add_question(
        db: Session, quiz_id: int, q_in: QuestionCreate, user_id: int, is_admin: bool = False
    ) -> QuestionAdminResponse:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to add questions to another organizer's quiz.",
            )

        def _add() -> Question:
            agg_max, agg_sum = (
                db.query(
                    func.coalesce(func.max(Question.order_index), 0),
                    func.coalesce(func.sum(Question.marks), 0),
                )
                .filter(Question.quiz_id == quiz_id)
                .one()
            )
            order_index = q_in.order_index if q_in.order_index > 0 else int(agg_max) + 1

            question = Question(
                quiz_id=quiz_id,
                question_text=q_in.question_text.strip(),
                option_a=q_in.option_a.strip(),
                option_b=q_in.option_b.strip(),
                option_c=q_in.option_c.strip(),
                option_d=q_in.option_d.strip(),
                correct_option=q_in.correct_option.upper(),
                marks=q_in.marks,
                explanation=q_in.explanation.strip() if q_in.explanation else None,
                topic=(q_in.topic or "General").strip(),
                difficulty=(q_in.difficulty or "medium").strip().lower(),
                learning_objective=(q_in.learning_objective or "").strip(),
                order_index=order_index,
            )
            db.add(question)
            quiz.total_marks = int(agg_sum) + int(q_in.marks)
            db.commit()
            db.refresh(question)
            return question

        question = execute_with_retry(db, _add)
        cache_manager.invalidate_quizzes(quiz_id=quiz_id)
        return QuestionAdminResponse.model_validate(question)

    @staticmethod
    def update_question(
        db: Session, question_id: int, q_in: QuestionUpdate, user_id: int, is_admin: bool = False
    ) -> QuestionAdminResponse:
        question = db.get(Question, question_id)
        if not question:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")

        quiz = db.get(Quiz, question.quiz_id)
        if not is_admin and quiz and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to update questions in another organizer's quiz.",
            )

        marks_changed = False
        if q_in.question_text is not None:
            question.question_text = q_in.question_text.strip()
        if q_in.option_a is not None:
            question.option_a = q_in.option_a.strip()
        if q_in.option_b is not None:
            question.option_b = q_in.option_b.strip()
        if q_in.option_c is not None:
            question.option_c = q_in.option_c.strip()
        if q_in.option_d is not None:
            question.option_d = q_in.option_d.strip()
        if q_in.correct_option is not None:
            question.correct_option = q_in.correct_option.upper()
        if q_in.marks is not None and q_in.marks != question.marks:
            question.marks = q_in.marks
            marks_changed = True
        if q_in.explanation is not None:
            question.explanation = q_in.explanation.strip() if q_in.explanation else None
        if q_in.topic is not None:
            question.topic = q_in.topic.strip() or "General"
        if q_in.difficulty is not None:
            question.difficulty = q_in.difficulty.strip().lower() or "medium"
        if q_in.learning_objective is not None:
            question.learning_objective = q_in.learning_objective.strip()
        if q_in.order_index is not None:
            question.order_index = q_in.order_index

        def _commit() -> None:
            db.flush()
            if quiz and marks_changed:
                quiz.total_marks = int(
                    db.query(func.coalesce(func.sum(Question.marks), 0))
                    .filter(Question.quiz_id == quiz.id)
                    .scalar()
                    or 0
                )
            db.commit()
            db.refresh(question)

        execute_with_retry(db, _commit)
        cache_manager.invalidate_quizzes(quiz_id=question.quiz_id)
        return QuestionAdminResponse.model_validate(question)

    @staticmethod
    def delete_question(db: Session, question_id: int, user_id: int, is_admin: bool = False) -> bool:
        question = db.get(Question, question_id)
        if not question:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question not found")

        quiz_id = question.quiz_id
        quiz = db.get(Quiz, quiz_id)
        if not is_admin and quiz and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to delete questions from another organizer's quiz.",
            )

        def _delete() -> None:
            db.delete(question)
            db.flush()
            if quiz:
                quiz.total_marks = int(
                    db.query(func.coalesce(func.sum(Question.marks), 0))
                    .filter(Question.quiz_id == quiz_id)
                    .scalar()
                    or 0
                )
                if quiz.total_marks == 0:
                    quiz.is_published = False
            db.commit()

        execute_with_retry(db, _delete)
        cache_manager.invalidate_quizzes(quiz_id=quiz_id)
        return True

    # ------------------ Optimized Listings & Statistics (Zero N+1) ------------------
    @staticmethod
    def get_admin_quizzes(
        db: Session,
        limit: Optional[int] = None,
        offset: int = 0,
        search: Optional[str] = None,
        return_total: bool = False,
    ) -> List[QuizAdminResponse] | Tuple[List[QuizAdminResponse], int]:
        page_limit = _clamp_limit(limit)
        page_offset = max(0, offset)

        q_counts_sq = (
            db.query(Question.quiz_id.label("qid"), func.count(Question.id).label("q_count"))
            .group_by(Question.quiz_id)
            .subquery()
        )
        a_counts_sq = (
            db.query(QuizAttempt.quiz_id.label("qid"), func.count(QuizAttempt.id).label("a_count"))
            .group_by(QuizAttempt.quiz_id)
            .subquery()
        )

        filters = []
        if search:
            s = f"%{search.strip().lower()}%"
            filters.append((func.lower(Quiz.title).like(s)) | (func.lower(Quiz.join_code).like(s)))

        query = (
            db.query(
                Quiz,
                func.coalesce(q_counts_sq.c.q_count, 0).label("question_count"),
                func.coalesce(a_counts_sq.c.a_count, 0).label("attempt_count"),
                User.full_name.label("creator_name"),
            )
            .outerjoin(q_counts_sq, q_counts_sq.c.qid == Quiz.id)
            .outerjoin(a_counts_sq, a_counts_sq.c.qid == Quiz.id)
            .outerjoin(User, User.id == Quiz.created_by_id)
        )
        if filters:
            query = query.filter(*filters)

        rows = (
            query.order_by(Quiz.created_at.desc(), Quiz.id.desc())
            .offset(page_offset)
            .limit(page_limit)
            .all()
        )

        items = [
            QuizService._format_admin_quiz(
                quiz,
                question_count=int(q_cnt or 0),
                attempt_count=int(a_cnt or 0),
                creator_name=creator_name,
            )
            for quiz, q_cnt, a_cnt, creator_name in rows
        ]

        if return_total:
            cnt_q = db.query(func.count(Quiz.id))
            if filters:
                cnt_q = cnt_q.filter(*filters)
            total = int(cnt_q.scalar() or 0)
            return items, total
        return items

    @staticmethod
    def get_organizer_quizzes(
        db: Session,
        organizer_id: int,
        limit: Optional[int] = None,
        offset: int = 0,
        return_total: bool = False,
    ) -> List[QuizAdminResponse] | Tuple[List[QuizAdminResponse], int]:
        page_limit = _clamp_limit(limit)
        page_offset = max(0, offset)

        q_counts_sq = (
            db.query(Question.quiz_id.label("qid"), func.count(Question.id).label("q_count"))
            .group_by(Question.quiz_id)
            .subquery()
        )
        a_counts_sq = (
            db.query(QuizAttempt.quiz_id.label("qid"), func.count(QuizAttempt.id).label("a_count"))
            .group_by(QuizAttempt.quiz_id)
            .subquery()
        )

        rows = (
            db.query(
                Quiz,
                func.coalesce(q_counts_sq.c.q_count, 0).label("question_count"),
                func.coalesce(a_counts_sq.c.a_count, 0).label("attempt_count"),
            )
            .outerjoin(q_counts_sq, q_counts_sq.c.qid == Quiz.id)
            .outerjoin(a_counts_sq, a_counts_sq.c.qid == Quiz.id)
            .filter(Quiz.created_by_id == organizer_id)
            .order_by(Quiz.created_at.desc(), Quiz.id.desc())
            .offset(page_offset)
            .limit(page_limit)
            .all()
        )

        items = [
            QuizService._format_admin_quiz(
                quiz,
                question_count=int(q_cnt or 0),
                attempt_count=int(a_cnt or 0),
                creator_name="You",
            )
            for quiz, q_cnt, a_cnt in rows
        ]

        if return_total:
            total = int(db.query(func.count(Quiz.id)).filter(Quiz.created_by_id == organizer_id).scalar() or 0)
            return items, total
        return items

    @staticmethod
    def get_organizer_dashboard_stats(db: Session, organizer_id: int) -> OrganizerDashboardStats:
        # Query 1: Quiz counts in SQL (zero row loading)
        quiz_row = (
            db.query(
                func.count(Quiz.id),
                func.coalesce(func.sum(case((Quiz.is_published == True, 1), else_=0)), 0),  # noqa: E712
            )
            .filter(Quiz.created_by_id == organizer_id)
            .one()
        )
        total_quizzes = int(quiz_row[0] or 0)
        published_quizzes = int(quiz_row[1] or 0)
        draft_quizzes = total_quizzes - published_quizzes

        if total_quizzes == 0:
            return OrganizerDashboardStats(
                total_quizzes=0,
                published_quizzes=0,
                draft_quizzes=0,
                total_participants=0,
                total_attempts=0,
                average_score=0.0,
            )

        # Query 2: Attempt aggregates via JOIN in SQL (zero row loading)
        att_row = (
            db.query(
                func.count(QuizAttempt.id),
                func.count(func.distinct(QuizAttempt.student_id)),
                func.avg(case((QuizAttempt.status.in_(FINISHED_STATUSES), QuizAttempt.percentage), else_=None)),
            )
            .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .filter(Quiz.created_by_id == organizer_id)
            .one()
        )

        return OrganizerDashboardStats(
            total_quizzes=total_quizzes,
            published_quizzes=published_quizzes,
            draft_quizzes=draft_quizzes,
            total_participants=int(att_row[1] or 0),
            total_attempts=int(att_row[0] or 0),
            average_score=round(float(att_row[2]), 1) if att_row[2] is not None else 0.0,
        )

    @staticmethod
    def get_student_quizzes(
        db: Session,
        student_id: Optional[int] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        subject: Optional[str] = None,
        search: Optional[str] = None,
        return_total: bool = False,
    ) -> List[QuizStudentResponse] | Tuple[List[QuizStudentResponse], int]:
        """Return paginated public published, non-disabled quizzes with question_count > 0 in 1 query."""
        page_limit = _clamp_limit(limit)
        page_offset = max(0, offset)

        # Cache shared public quiz catalog metadata when no search/subject filter is applied
        use_cache = (search is None and subject is None and not return_total)
        cache_key = f"quizzes:public:l{page_limit}:o{page_offset}"

        base_items: Optional[List[Dict[str, Any]]] = None
        if use_cache:
            base_items = cache_manager.get(cache_key)

        if base_items is None:
            q_counts_sq = (
                db.query(Question.quiz_id.label("qid"), func.count(Question.id).label("q_count"))
                .group_by(Question.quiz_id)
                .subquery()
            )

            filters = [
                Quiz.is_published == True,  # noqa: E712
                Quiz.is_public == True,  # noqa: E712
                Quiz.is_disabled == False,  # noqa: E712
            ]
            if subject:
                filters.append(func.lower(Quiz.subject) == subject.strip().lower())
            if search:
                s = f"%{search.strip().lower()}%"
                filters.append((func.lower(Quiz.title).like(s)) | (func.lower(Quiz.join_code).like(s)))

            # Single query with INNER JOIN to question counts (ensuring count > 0) + LEFT JOIN creator
            base_query = (
                db.query(
                    Quiz.id,
                    Quiz.title,
                    Quiz.subject,
                    Quiz.description,
                    Quiz.instructions,
                    Quiz.duration_minutes,
                    Quiz.passing_percentage,
                    Quiz.total_marks,
                    q_counts_sq.c.q_count.label("question_count"),
                    Quiz.join_code,
                    Quiz.is_public,
                    User.full_name.label("creator_name"),
                    Quiz.max_attempts,
                    Quiz.results_released,
                )
                .join(q_counts_sq, q_counts_sq.c.qid == Quiz.id)
                .outerjoin(User, User.id == Quiz.created_by_id)
                .filter(*filters)
            )

            rows = base_query.order_by(Quiz.id.asc()).offset(page_offset).limit(page_limit).all()

            base_items = [
                {
                    "id": r.id,
                    "title": r.title,
                    "subject": r.subject or "General",
                    "description": r.description,
                    "instructions": r.instructions,
                    "duration_minutes": r.duration_minutes,
                    "passing_percentage": r.passing_percentage,
                    "total_marks": r.total_marks,
                    "question_count": int(r.question_count or 0),
                    "join_code": r.join_code,
                    "is_public": r.is_public,
                    "creator_name": r.creator_name or "Organizer",
                    "max_attempts": r.max_attempts,
                    "results_released": r.results_released,
                }
                for r in rows
            ]

            if use_cache:
                cache_manager.set(cache_key, base_items, ttl=settings.CACHE_QUIZ_LIST_TTL)

        # Fetch student's personal attempt counts in ONE batched query (never cached under shared keys!)
        attempts_map: Dict[int, int] = {}
        if student_id and base_items:
            quiz_ids = [item["id"] for item in base_items]
            att_rows = (
                db.query(QuizAttempt.quiz_id, func.count(QuizAttempt.id))
                .filter(QuizAttempt.student_id == student_id, QuizAttempt.quiz_id.in_(quiz_ids))
                .group_by(QuizAttempt.quiz_id)
                .all()
            )
            attempts_map = {int(qid): int(cnt) for qid, cnt in att_rows}

        results = [
            QuizStudentResponse(**item, attempts_taken=attempts_map.get(item["id"], 0))
            for item in base_items
        ]

        if return_total:
            q_counts_sq = (
                db.query(Question.quiz_id.label("qid"))
                .group_by(Question.quiz_id)
                .subquery()
            )
            cnt_q = (
                db.query(func.count(Quiz.id))
                .join(q_counts_sq, q_counts_sq.c.qid == Quiz.id)
                .filter(
                    Quiz.is_published == True,  # noqa: E712
                    Quiz.is_public == True,  # noqa: E712
                    Quiz.is_disabled == False,  # noqa: E712
                )
            )
            if subject:
                cnt_q = cnt_q.filter(func.lower(Quiz.subject) == subject.strip().lower())
            if search:
                s = f"%{search.strip().lower()}%"
                cnt_q = cnt_q.filter((func.lower(Quiz.title).like(s)) | (func.lower(Quiz.join_code).like(s)))
            total = int(cnt_q.scalar() or 0)
            return results, total

        return results

    @staticmethod
    def get_quiz_details(
        db: Session,
        quiz_id: int,
        current_user: Optional[User] = None,
    ) -> QuizDetailStudentResponse:
        """Direct PK lookup (1 query instead of 6N+2!) with visibility authorization."""
        q_counts_sq = (
            db.query(Question.quiz_id.label("qid"), func.count(Question.id).label("q_count"))
            .filter(Question.quiz_id == quiz_id)
            .group_by(Question.quiz_id)
            .subquery()
        )
        row = (
            db.query(
                Quiz,
                func.coalesce(q_counts_sq.c.q_count, 0).label("question_count"),
                User.full_name.label("creator_name"),
            )
            .outerjoin(q_counts_sq, q_counts_sq.c.qid == Quiz.id)
            .outerjoin(User, User.id == Quiz.created_by_id)
            .filter(Quiz.id == quiz_id)
            .first()
        )
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        quiz, q_count, creator_name = row

        # Restrict unpublished/disabled quizzes to admin or quiz creator
        is_privileged = (
            current_user is not None
            and (current_user.role == "admin" or quiz.created_by_id == current_user.id)
        )
        if (not quiz.is_published or quiz.is_disabled) and not is_privileged:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        attempts_taken = 0
        if current_user is not None:
            attempts_taken = int(
                db.query(func.count(QuizAttempt.id))
                .filter(QuizAttempt.quiz_id == quiz.id, QuizAttempt.student_id == current_user.id)
                .scalar()
                or 0
            )

        return QuizDetailStudentResponse(
            id=quiz.id,
            title=quiz.title,
            subject=quiz.subject or "General",
            description=quiz.description,
            instructions=quiz.instructions,
            duration_minutes=quiz.duration_minutes,
            passing_percentage=quiz.passing_percentage,
            total_marks=quiz.total_marks,
            question_count=int(q_count or 0),
            join_code=quiz.join_code,
            is_public=quiz.is_public,
            creator_name=creator_name or "Organizer",
            max_attempts=quiz.max_attempts,
            attempts_taken=attempts_taken,
            results_released=quiz.results_released,
        )

    @staticmethod
    def get_quiz_by_join_code(db: Session, join_code: str, student_id: Optional[int] = None) -> QuizStudentResponse:
        """Find a quiz by its private or public join code and validate eligibility."""
        clean_code = join_code.strip().upper()
        row = (
            db.query(Quiz, User.full_name.label("creator_name"))
            .outerjoin(User, User.id == Quiz.created_by_id)
            .filter(Quiz.join_code == clean_code)
            .first()
        )
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No quiz found matching this join code.")

        quiz, creator_name = row

        if not quiz.is_published:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This quiz is currently in draft mode and not available.",
            )

        if quiz.is_disabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This quiz has been disabled or closed by platform administrators.",
            )

        now = datetime.now(timezone.utc)
        st = quiz.start_time.replace(tzinfo=timezone.utc) if (quiz.start_time and quiz.start_time.tzinfo is None) else quiz.start_time
        et = quiz.end_time.replace(tzinfo=timezone.utc) if (quiz.end_time and quiz.end_time.tzinfo is None) else quiz.end_time

        if st and now < st:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"This assessment opens at {st.strftime('%Y-%m-%d %H:%M UTC')}.",
            )

        if et and now > et:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The deadline for this assessment has passed.",
            )

        count = int(db.query(func.count(Question.id)).filter(Question.quiz_id == quiz.id).scalar() or 0)
        attempts_taken = 0
        if student_id:
            attempts_taken = int(
                db.query(func.count(QuizAttempt.id))
                .filter(QuizAttempt.quiz_id == quiz.id, QuizAttempt.student_id == student_id)
                .scalar()
                or 0
            )

        return QuizStudentResponse(
            id=quiz.id,
            title=quiz.title,
            subject=quiz.subject or "General",
            description=quiz.description,
            instructions=quiz.instructions,
            duration_minutes=quiz.duration_minutes,
            passing_percentage=quiz.passing_percentage,
            total_marks=quiz.total_marks,
            question_count=count,
            join_code=quiz.join_code,
            is_public=quiz.is_public,
            creator_name=creator_name or "Organizer",
            max_attempts=quiz.max_attempts,
            attempts_taken=attempts_taken,
            results_released=quiz.results_released,
        )

    @staticmethod
    def get_quiz_report(
        db: Session,
        quiz_id: int,
        user_id: int,
        is_admin: bool = False,
        attempts_limit: Optional[int] = None,
        attempts_offset: int = 0,
    ) -> QuizReportResponse:
        """
        High-performance quiz analytics & misconception detector report.
        Uses 4 constant SQL queries regardless of number of attempts or questions (ZERO N+1!).
        """
        # Query 1: Quiz metadata + ownership check
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to another organizer's reports.",
            )

        page_limit = _clamp_limit(attempts_limit)
        page_offset = max(0, attempts_offset)

        # Query 2: Attempt summary statistics aggregated entirely in SQL
        att_agg = (
            db.query(
                func.count(QuizAttempt.id).label("total_attempts"),
                func.count(func.distinct(QuizAttempt.student_id)).label("unique_participants"),
                func.coalesce(func.sum(case((QuizAttempt.status.in_(FINISHED_STATUSES), 1), else_=0)), 0).label("finished_count"),
                func.coalesce(
                    func.sum(
                        case(((QuizAttempt.status.in_(FINISHED_STATUSES)) & (QuizAttempt.is_passed == True), 1), else_=0)  # noqa: E712
                    ),
                    0,
                ).label("pass_count"),
                func.avg(case((QuizAttempt.status.in_(FINISHED_STATUSES), QuizAttempt.percentage), else_=None)).label("avg_score"),
                func.max(case((QuizAttempt.status.in_(FINISHED_STATUSES), QuizAttempt.percentage), else_=None)).label("hi_score"),
                func.min(case((QuizAttempt.status.in_(FINISHED_STATUSES), QuizAttempt.percentage), else_=None)).label("lo_score"),
            )
            .filter(QuizAttempt.quiz_id == quiz_id)
            .one()
        )

        total_attempts = int(att_agg.total_attempts or 0)
        unique_participants = int(att_agg.unique_participants or 0)
        finished_count = int(att_agg.finished_count or 0)
        pass_count = int(att_agg.pass_count or 0)
        fail_count = finished_count - pass_count
        avg_score = round(float(att_agg.avg_score), 1) if att_agg.avg_score is not None else 0.0
        hi_score = float(att_agg.hi_score) if att_agg.hi_score is not None else 0.0
        lo_score = float(att_agg.lo_score) if att_agg.lo_score is not None else 0.0
        pass_pct = round((pass_count / finished_count) * 100.0, 1) if finished_count > 0 else 0.0

        # Query 3: Paginated participant attempts joined with User (ZERO N+1!)
        attempt_rows = (
            db.query(
                QuizAttempt.id,
                QuizAttempt.student_id,
                User.full_name.label("student_name"),
                User.email.label("student_email"),
                QuizAttempt.status,
                QuizAttempt.score_obtained,
                QuizAttempt.total_marks,
                QuizAttempt.percentage,
                QuizAttempt.is_passed,
                QuizAttempt.end_time,
                QuizAttempt.created_at,
            )
            .outerjoin(User, User.id == QuizAttempt.student_id)
            .filter(QuizAttempt.quiz_id == quiz_id)
            .order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
            .offset(page_offset)
            .limit(page_limit)
            .all()
        )

        attempt_items = [
            ParticipantAttemptItem(
                attempt_id=r.id,
                student_id=r.student_id,
                student_name=r.student_name or "Unknown",
                student_email=r.student_email or "N/A",
                status=r.status,
                score_obtained=r.score_obtained,
                total_marks=r.total_marks,
                percentage=r.percentage,
                is_passed=r.is_passed,
                submitted_at=r.end_time or r.created_at,
            )
            for r in attempt_rows
        ]
        attempts_has_more = (page_offset + len(attempt_items)) < total_attempts

        # Query 4: Questions for this quiz
        questions = (
            db.query(Question)
            .filter(Question.quiz_id == quiz_id)
            .order_by(Question.order_index.asc(), Question.id.asc())
            .all()
        )

        # Query 5: Single GROUP BY aggregation across ALL AttemptAnswers for this quiz (ZERO per-question queries!)
        answer_stats_by_q: Dict[int, Dict[str, Any]] = {}
        if questions:
            q_ids = [q.id for q in questions]
            grouped_rows = (
                db.query(
                    AttemptAnswer.question_id,
                    AttemptAnswer.selected_option,
                    AttemptAnswer.is_correct,
                    func.count(AttemptAnswer.id).label("cnt"),
                )
                .filter(AttemptAnswer.question_id.in_(q_ids))
                .group_by(
                    AttemptAnswer.question_id,
                    AttemptAnswer.selected_option,
                    AttemptAnswer.is_correct,
                )
                .all()
            )
            for qid, sel_opt, is_corr, cnt in grouped_rows:
                bucket = answer_stats_by_q.setdefault(
                    qid, {"total": 0, "correct": 0, "wrongs": {"A": 0, "B": 0, "C": 0, "D": 0}}
                )
                cnt_int = int(cnt or 0)
                bucket["total"] += cnt_int
                if is_corr:
                    bucket["correct"] += cnt_int
                elif sel_opt in bucket["wrongs"]:
                    bucket["wrongs"][sel_opt] += cnt_int

        q_stats: List[QuestionPerformanceStat] = []
        topic_aggregates: Dict[str, Dict[str, int]] = {}
        misconceptions_list: List[Dict[str, Any]] = []

        for q in questions:
            bucket = answer_stats_by_q.get(
                q.id, {"total": 0, "correct": 0, "wrongs": {"A": 0, "B": 0, "C": 0, "D": 0}}
            )
            tot_ans = bucket["total"]
            correct_ans = bucket["correct"]
            acc_pct = round((correct_ans / tot_ans) * 100.0, 1) if tot_ans > 0 else 0.0

            common_wrong_opt = None
            common_wrong_cnt = 0
            common_wrong_pct = 0.0
            misconception_note = None

            if tot_ans > 0:
                sorted_wrongs = sorted(bucket["wrongs"].items(), key=lambda x: x[1], reverse=True)
                if sorted_wrongs and sorted_wrongs[0][1] > 0:
                    common_wrong_opt, common_wrong_cnt = sorted_wrongs[0]
                    common_wrong_pct = round((common_wrong_cnt / tot_ans) * 100.0, 1)

                    if common_wrong_pct >= 25.0 and acc_pct < 70.0:
                        misconception_note = (
                            f"Identified distractor pattern: {common_wrong_pct}% of students selected Option {common_wrong_opt} "
                            f"instead of correct Option {q.correct_option}. Indicates conceptual confusion on {q.topic or 'this concept'}."
                        )
                        misconceptions_list.append(
                            {
                                "question_id": q.id,
                                "question_text": q.question_text,
                                "topic": q.topic or "General",
                                "correct_option": q.correct_option,
                                "common_distractor": common_wrong_opt,
                                "distractor_percentage": common_wrong_pct,
                                "accuracy_percentage": acc_pct,
                                "explanation": q.explanation or "Review core definition and contrast with distractor option.",
                                "recommended_action": f"Review {q.topic or 'this concept'} specifically clarifying why Option {common_wrong_opt} is incorrect.",
                            }
                        )

            q_topic = q.topic or "General"
            t_agg = topic_aggregates.setdefault(
                q_topic, {"total_questions": 0, "total_answers": 0, "correct_answers": 0}
            )
            t_agg["total_questions"] += 1
            t_agg["total_answers"] += tot_ans
            t_agg["correct_answers"] += correct_ans

            q_stats.append(
                QuestionPerformanceStat(
                    question_id=q.id,
                    question_text=q.question_text,
                    total_answers=tot_ans,
                    correct_answers=correct_ans,
                    accuracy_percentage=acc_pct,
                    topic=q_topic,
                    difficulty=q.difficulty or "medium",
                    common_wrong_option=common_wrong_opt,
                    common_wrong_count=common_wrong_cnt,
                    common_wrong_percentage=common_wrong_pct,
                    misconception_note=misconception_note,
                )
            )

        topic_breakdown = []
        for t_name, t_data in topic_aggregates.items():
            t_acc = (
                round((t_data["correct_answers"] / t_data["total_answers"]) * 100.0, 1)
                if t_data["total_answers"] > 0
                else 0.0
            )
            if t_acc < 50.0:
                t_status = "🚨 Critical Misconception / High Need"
            elif t_acc < 75.0:
                t_status = "⚠️ Needs Practice / Review"
            else:
                t_status = "✅ Concept Mastered"

            topic_breakdown.append(
                {
                    "topic": t_name,
                    "question_count": t_data["total_questions"],
                    "total_answers": t_data["total_answers"],
                    "accuracy_percentage": t_acc,
                    "status": t_status,
                }
            )

        recommended_actions = []
        for tb in topic_breakdown:
            if tb["accuracy_percentage"] < 65.0 and tb["total_answers"] > 0:
                recommended_actions.append(
                    {
                        "priority": "HIGH",
                        "category": "Topic Revision",
                        "title": f"Schedule Revision: {tb['topic']}",
                        "description": f"Class accuracy on {tb['topic']} is {tb['accuracy_percentage']}%. Recommend a 10-minute targeted review before next exam.",
                        "action_type": "revision_review",
                        "topic": tb["topic"],
                    }
                )

        struggling_students = [
            a for a in attempt_items if not a.is_passed and a.status in FINISHED_STATUSES
        ]
        if struggling_students:
            names = ", ".join({s.student_name for s in struggling_students[:3]})
            recommended_actions.append(
                {
                    "priority": "MEDIUM",
                    "category": "Student Support",
                    "title": f"Follow-up with {fail_count} Struggling Student(s)",
                    "description": f"Students needing additional support: {names}. Consider offering practice attempt or 1-on-1 feedback.",
                    "action_type": "student_outreach",
                    "count": fail_count,
                }
            )

        if misconceptions_list:
            recommended_actions.append(
                {
                    "priority": "HIGH",
                    "category": "Misconception Alert",
                    "title": f"Clarify {len(misconceptions_list)} Common Exam Distractor(s)",
                    "description": f"Significant clustering on wrong answer options detected in {len(misconceptions_list)} question(s). Open Misconception Detector tab to inspect details.",
                    "action_type": "distractor_clarification",
                    "count": len(misconceptions_list),
                }
            )

        if not recommended_actions and total_attempts > 0:
            recommended_actions.append(
                {
                    "priority": "INFO",
                    "category": "Class Achievement",
                    "title": "High Overall Mastery Achieved",
                    "description": f"The class achieved an average score of {avg_score}%. Core objectives are well understood.",
                    "action_type": "celebrate",
                }
            )

        return QuizReportResponse(
            quiz_id=quiz.id,
            title=quiz.title,
            subject=quiz.subject or "General",
            join_code=quiz.join_code,
            is_published=quiz.is_published,
            results_released=quiz.results_released,
            total_marks=quiz.total_marks,
            passing_percentage=quiz.passing_percentage,
            total_attempts=total_attempts,
            unique_participants=unique_participants,
            average_score=avg_score,
            highest_score=hi_score,
            lowest_score=lo_score,
            pass_count=pass_count,
            fail_count=fail_count,
            pass_percentage=pass_pct,
            attempts=attempt_items,
            attempts_limit=page_limit,
            attempts_offset=page_offset,
            attempts_has_more=attempts_has_more,
            question_stats=q_stats,
            misconceptions=misconceptions_list,
            topic_breakdown=topic_breakdown,
            recommended_actions=recommended_actions,
        )

    @staticmethod
    def export_quiz_csv(db: Session, quiz_id: int, user_id: int, is_admin: bool = False) -> str:
        """Stream quiz attempt results into CSV using a single JOIN query (zero N+1)."""
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to another organizer's reports.",
            )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "Attempt ID",
                "Student Name",
                "Student Email",
                "Score Obtained",
                "Total Marks",
                "Percentage",
                "Passed",
                "Status",
                "Submitted Date (UTC)",
            ]
        )

        rows = (
            db.query(
                QuizAttempt.id,
                User.full_name,
                User.email,
                QuizAttempt.score_obtained,
                QuizAttempt.total_marks,
                QuizAttempt.percentage,
                QuizAttempt.is_passed,
                QuizAttempt.status,
                QuizAttempt.end_time,
                QuizAttempt.created_at,
            )
            .outerjoin(User, User.id == QuizAttempt.student_id)
            .filter(QuizAttempt.quiz_id == quiz_id)
            .order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
            .yield_per(500)
        )

        for r in rows:
            ts = r.end_time or r.created_at
            writer.writerow(
                [
                    r.id,
                    r.full_name or "Unknown",
                    r.email or "N/A",
                    r.score_obtained,
                    r.total_marks,
                    f"{r.percentage}%",
                    "YES" if r.is_passed else "NO",
                    r.status,
                    ts.strftime("%Y-%m-%d %H:%M:%S") if ts else "In Progress",
                ]
            )

        return output.getvalue()

    @staticmethod
    def get_quiz_questions_admin(
        db: Session,
        quiz_id: int,
        user_id: Optional[int] = None,
        is_admin: bool = True,
        limit: Optional[int] = None,
        offset: int = 0,
        return_total: bool = False,
    ) -> List[QuestionAdminResponse] | Tuple[List[QuestionAdminResponse], int]:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not is_admin and user_id is not None and quiz.created_by_id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to view questions of another organizer's quiz.",
            )

        page_limit = _clamp_limit(limit, default=settings.MAX_PAGE_SIZE)
        page_offset = max(0, offset)

        questions = (
            db.query(Question)
            .filter(Question.quiz_id == quiz_id)
            .order_by(Question.order_index.asc(), Question.id.asc())
            .offset(page_offset)
            .limit(page_limit)
            .all()
        )
        items = [QuestionAdminResponse.model_validate(q) for q in questions]

        if return_total:
            total = int(db.query(func.count(Question.id)).filter(Question.quiz_id == quiz_id).scalar() or 0)
            return items, total
        return items

    @staticmethod
    def _format_admin_quiz(
        quiz: Quiz,
        question_count: int,
        attempt_count: int = 0,
        creator_name: Optional[str] = None,
    ) -> QuizAdminResponse:
        return QuizAdminResponse(
            id=quiz.id,
            title=quiz.title,
            subject=quiz.subject or "General",
            description=quiz.description,
            instructions=quiz.instructions,
            duration_minutes=quiz.duration_minutes,
            passing_percentage=quiz.passing_percentage,
            total_marks=quiz.total_marks,
            is_published=quiz.is_published,
            join_code=quiz.join_code,
            is_public=quiz.is_public,
            max_attempts=quiz.max_attempts,
            release_results_immediately=quiz.release_results_immediately,
            results_released=quiz.results_released,
            start_time=quiz.start_time,
            end_time=quiz.end_time,
            is_disabled=quiz.is_disabled,
            created_by_id=quiz.created_by_id,
            creator_name=creator_name,
            created_at=quiz.created_at,
            updated_at=quiz.updated_at,
            question_count=question_count,
            attempt_count=attempt_count,
        )
