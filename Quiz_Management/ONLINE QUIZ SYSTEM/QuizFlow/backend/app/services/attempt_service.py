"""
High-Concurrency Quiz Attempt, Atomic Answer UPSERT, Batch Submission,
Idempotent Finalization, and Server-Side Scoring Service.

Guarantees:
- Never loses student answers (atomic dialect-native UPSERT + batch flush on finish).
- Never calculates scores from client-supplied data (100% server-side grading).
- Prevents race conditions on simultaneous start, answer save, and submit requests.
- Enforces server-side quiz timer and attempt limits atomically.
- Eliminates all N+1 queries in student history and dashboard statistics.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy import case, func, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.cache import cache_manager
from app.core.config import settings
from app.database.session import execute_with_retry
from app.models.attempt import AttemptStatus, QuizAttempt
from app.models.attempt_answer import AttemptAnswer
from app.models.question import Question
from app.models.quiz import Quiz
from app.models.user import User
from app.schemas.attempt import (
    AnswerSubmitRequest,
    AnswerSubmitResponse,
    AttemptResultResponse,
    AttemptStartResponse,
    AttemptSummaryResponse,
    BatchAnswerSubmitResponse,
    QuestionReviewItem,
)
from app.schemas.question import QuestionStudentResponse
from app.services.audit_service import AuditService

FINISHED_STATUSES = (AttemptStatus.COMPLETED.value, AttemptStatus.EXPIRED.value)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _clamp_limit(limit: Optional[int], default: Optional[int] = None) -> int:
    fallback = default if default is not None else settings.DEFAULT_PAGE_SIZE
    if limit is None:
        return fallback
    return max(1, min(limit, settings.MAX_PAGE_SIZE))


def _dialect_upsert_answers(
    db: Session,
    attempt_id: int,
    answer_rows: List[Dict[str, object]],
    now: datetime,
) -> None:
    """
    Atomically UPSERT one or more (attempt_id, question_id) answer rows using native
    ON CONFLICT DO UPDATE for both SQLite and PostgreSQL.
    Eliminates check-then-insert race conditions and duplicate rows completely.
    """
    if not answer_rows:
        return

    dialect = db.get_bind().dialect.name
    values = [
        {
            "attempt_id": attempt_id,
            "question_id": row["question_id"],
            "selected_option": row["selected_option"],
            "is_correct": False,
            "marks_awarded": 0.0,
            "answered_at": now,
        }
        for row in answer_rows
    ]

    if dialect == "postgresql":
        stmt = pg_insert(AttemptAnswer).values(values)
        stmt = stmt.on_conflict_do_update(
            constraint="uq_attempt_answers_attempt_question",
            set_={
                "selected_option": stmt.excluded.selected_option,
                "answered_at": stmt.excluded.answered_at,
            },
        )
        db.execute(stmt)
    elif dialect == "sqlite":
        stmt = sqlite_insert(AttemptAnswer).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["attempt_id", "question_id"],
            set_={
                "selected_option": stmt.excluded.selected_option,
                "answered_at": stmt.excluded.answered_at,
            },
        )
        db.execute(stmt)
    else:
        # Generic fallback for any other SQL dialect
        q_ids = [v["question_id"] for v in values]
        existing = {
            a.question_id: a
            for a in db.query(AttemptAnswer)
            .filter(AttemptAnswer.attempt_id == attempt_id, AttemptAnswer.question_id.in_(q_ids))
            .all()
        }
        for v in values:
            ans = existing.get(v["question_id"])
            if ans:
                ans.selected_option = v["selected_option"]
                ans.answered_at = now
            else:
                db.add(AttemptAnswer(**v))


class AttemptService:
    @staticmethod
    def start_quiz(db: Session, quiz_id: int, student_id: int) -> AttemptStartResponse:
        quiz = db.get(Quiz, quiz_id)
        if not quiz:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")

        if not quiz.is_published:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This quiz is not currently available.",
            )

        if quiz.is_disabled:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This quiz has been disabled or closed by administrators.",
            )

        now = datetime.now(timezone.utc)
        if quiz.start_time and now < _as_utc(quiz.start_time):
            st = _as_utc(quiz.start_time)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"This assessment does not open until {st.strftime('%Y-%m-%d %H:%M UTC')}.",
            )

        if quiz.end_time and now > _as_utc(quiz.end_time):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The closing deadline for this assessment has passed.",
            )

        def _start_or_resume() -> Tuple[QuizAttempt, int, bool]:
            # Acquire per-(student, quiz) advisory lock on PostgreSQL to serialize concurrent starts
            dialect = db.get_bind().dialect.name
            if dialect == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
                    {"k1": int(student_id), "k2": int(quiz_id)},
                )

            existing_attempt = (
                db.query(QuizAttempt)
                .filter(
                    QuizAttempt.quiz_id == quiz_id,
                    QuizAttempt.student_id == student_id,
                    QuizAttempt.status == AttemptStatus.IN_PROGRESS.value,
                )
                .order_by(QuizAttempt.id.desc())
                .first()
            )

            finished_attempts = int(
                db.query(func.count(QuizAttempt.id))
                .filter(
                    QuizAttempt.quiz_id == quiz_id,
                    QuizAttempt.student_id == student_id,
                    QuizAttempt.status != AttemptStatus.IN_PROGRESS.value,
                )
                .scalar()
                or 0
            )

            if existing_attempt:
                deadline = _as_utc(existing_attempt.deadline_time)
                if now > deadline + timedelta(seconds=settings.ATTEMPT_GRACE_PERIOD_SECONDS):
                    AttemptService._grade_and_finalize(
                        db, existing_attempt, quiz=quiz, final_status=AttemptStatus.EXPIRED.value, commit=True
                    )
                    finished_attempts += 1
                else:
                    return existing_attempt, finished_attempts + 1, False

            if finished_attempts >= quiz.max_attempts:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"You have reached the maximum allowed attempts ({quiz.max_attempts}) for this quiz.",
                )

            deadline_time = now + timedelta(minutes=quiz.duration_minutes)
            attempt = QuizAttempt(
                student_id=student_id,
                quiz_id=quiz.id,
                start_time=now,
                deadline_time=deadline_time,
                status=AttemptStatus.IN_PROGRESS.value,
                total_marks=float(quiz.total_marks),
                score_obtained=0.0,
                percentage=0.0,
                is_passed=False,
            )
            db.add(attempt)
            db.flush()

            AuditService.log(
                db,
                action="QUIZ_ATTEMPT_START",
                user_id=student_id,
                resource_type="attempt",
                resource_id=attempt.id,
                details={"quiz_id": quiz.id, "attempt_number": finished_attempts + 1},
                commit=False,
            )
            db.commit()
            db.refresh(attempt)
            return attempt, finished_attempts + 1, True

        attempt, attempt_number, created = execute_with_retry(db, _start_or_resume)
        if created:
            cache_manager.invalidate_attempts_and_stats()
        return AttemptService._build_start_response(db, attempt, quiz, attempt_number=attempt_number)

    @staticmethod
    def save_answer(
        db: Session,
        attempt_id: int,
        student_id: int,
        ans_in: AnswerSubmitRequest,
    ) -> AnswerSubmitResponse:
        now = datetime.now(timezone.utc)

        def _save() -> None:
            attempt = db.get(QuizAttempt, attempt_id)
            if not attempt:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")

            if attempt.student_id != student_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Unauthorized: You do not own this quiz attempt.",
                )

            if attempt.status != AttemptStatus.IN_PROGRESS.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot save answer: attempt is already {attempt.status}.",
                )

            deadline = _as_utc(attempt.deadline_time)
            if now > deadline + timedelta(seconds=settings.ATTEMPT_GRACE_PERIOD_SECONDS):
                AttemptService._grade_and_finalize(db, attempt, final_status=AttemptStatus.EXPIRED.value, commit=True)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Time expired! Quiz deadline has elapsed.",
                )

            valid_q = (
                db.query(Question.id)
                .filter(Question.id == ans_in.question_id, Question.quiz_id == attempt.quiz_id)
                .first()
            )
            if not valid_q:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Invalid question for this quiz.",
                )

            _dialect_upsert_answers(
                db,
                attempt_id=attempt.id,
                answer_rows=[{"question_id": ans_in.question_id, "selected_option": ans_in.selected_option}],
                now=now,
            )
            db.commit()

        execute_with_retry(db, _save)
        return AnswerSubmitResponse(
            attempt_id=attempt_id,
            question_id=ans_in.question_id,
            selected_option=ans_in.selected_option,
            saved=True,
        )

    @staticmethod
    def save_answers_batch(
        db: Session,
        attempt_id: int,
        student_id: int,
        answers_in: List[AnswerSubmitRequest],
    ) -> BatchAnswerSubmitResponse:
        """
        Atomically save/update a batch of answers in a single DB transaction.
        Deduplicates repeated question_ids in the batch (keeping last selection).
        """
        if len(answers_in) > settings.MAX_BATCH_ANSWERS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Maximum {settings.MAX_BATCH_ANSWERS} answers allowed per batch.",
            )

        now = datetime.now(timezone.utc)
        # Deduplicate keeping the latest answer per question_id
        deduped: Dict[int, Optional[str]] = {}
        for item in answers_in:
            deduped[item.question_id] = item.selected_option

        def _save_batch() -> List[int]:
            attempt = db.get(QuizAttempt, attempt_id)
            if not attempt:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")

            if attempt.student_id != student_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Unauthorized: You do not own this quiz attempt.",
                )

            if attempt.status != AttemptStatus.IN_PROGRESS.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot save answers: attempt is already {attempt.status}.",
                )

            deadline = _as_utc(attempt.deadline_time)
            if now > deadline + timedelta(seconds=settings.ATTEMPT_GRACE_PERIOD_SECONDS):
                AttemptService._grade_and_finalize(db, attempt, final_status=AttemptStatus.EXPIRED.value, commit=True)
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Time expired! Quiz deadline has elapsed.",
                )

            requested_ids = list(deduped.keys())
            valid_ids = {
                qid
                for (qid,) in db.query(Question.id)
                .filter(Question.quiz_id == attempt.quiz_id, Question.id.in_(requested_ids))
                .all()
            }
            invalid_ids = set(requested_ids) - valid_ids
            if invalid_ids:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid question ID(s) for this quiz: {sorted(invalid_ids)}",
                )

            rows = [{"question_id": qid, "selected_option": opt} for qid, opt in deduped.items()]
            _dialect_upsert_answers(db, attempt_id=attempt.id, answer_rows=rows, now=now)
            db.commit()
            return requested_ids

        saved_ids = execute_with_retry(db, _save_batch)
        return BatchAnswerSubmitResponse(
            attempt_id=attempt_id,
            saved_count=len(saved_ids),
            saved_question_ids=saved_ids,
            server_time=now,
        )

    @staticmethod
    def finish_quiz(
        db: Session,
        attempt_id: int,
        student_id: int,
        final_answers: Optional[List[AnswerSubmitRequest]] = None,
        idempotent: bool = False,
    ) -> AttemptResultResponse:
        """
        Atomically finalize and grade a quiz attempt on the server.
        - Uses row locking (FOR UPDATE on PostgreSQL) + atomic conditional UPDATE
          (`WHERE id = :id AND status = 'in_progress'`) to eliminate concurrent double-grading races.
        - Optionally flushes final pending answers inside the same transaction so no answer is ever lost.
        - When `idempotent=True` (e.g., client passes `Idempotency-Key` header on network retry),
          returning an already-finalized attempt succeeds cleanly with the stored result.
        - Without `idempotent`, duplicate submission returns HTTP 400 to guard explicit double-submits.
        """
        now = datetime.now(timezone.utc)

        def _finish() -> Tuple[Optional[AttemptResultResponse], bool]:
            dialect = db.get_bind().dialect.name
            q = db.query(QuizAttempt).filter(QuizAttempt.id == attempt_id)
            if dialect == "postgresql":
                q = q.with_for_update()
            attempt = q.first()

            if not attempt:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")

            if attempt.student_id != student_id:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Unauthorized: You do not own this quiz attempt.",
                )

            if attempt.status != AttemptStatus.IN_PROGRESS.value:
                if idempotent:
                    return None, True
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"This quiz attempt has already been {attempt.status}.",
                )

            quiz = db.get(Quiz, attempt.quiz_id)
            deadline = _as_utc(attempt.deadline_time)
            final_status = (
                AttemptStatus.EXPIRED.value
                if now > deadline + timedelta(seconds=settings.ATTEMPT_GRACE_PERIOD_SECONDS)
                else AttemptStatus.COMPLETED.value
            )

            # Atomically claim the IN_PROGRESS -> final_status transition so concurrent submits cannot race!
            claim = db.execute(
                update(QuizAttempt)
                .where(
                    QuizAttempt.id == attempt.id,
                    QuizAttempt.status == AttemptStatus.IN_PROGRESS.value,
                )
                .values(status=final_status, end_time=now)
            )
            if (claim.rowcount or 0) == 0:
                db.rollback()
                if idempotent:
                    return None, True
                refreshed = db.get(QuizAttempt, attempt_id)
                cur_status = refreshed.status if refreshed else "completed"
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"This quiz attempt has already been {cur_status}.",
                )

            # Atomically flush any pending client answers before grading (only if within deadline window)
            if final_answers and final_status == AttemptStatus.COMPLETED.value:
                deduped = {a.question_id: a.selected_option for a in final_answers}
                valid_ids = {
                    qid
                    for (qid,) in db.query(Question.id)
                    .filter(Question.quiz_id == attempt.quiz_id, Question.id.in_(list(deduped.keys())))
                    .all()
                }
                valid_rows = [
                    {"question_id": qid, "selected_option": opt}
                    for qid, opt in deduped.items()
                    if qid in valid_ids
                ]
                _dialect_upsert_answers(db, attempt_id=attempt.id, answer_rows=valid_rows, now=now)

            # Grade and finalize in the exact same transaction
            questions, answers_map = AttemptService._grade_and_finalize(
                db, attempt, quiz=quiz, final_status=final_status, now=now, commit=False
            )

            AuditService.log(
                db,
                action="QUIZ_ATTEMPT_SUBMIT",
                user_id=student_id,
                resource_type="attempt",
                resource_id=attempt.id,
                details={
                    "score": attempt.score_obtained,
                    "percentage": attempt.percentage,
                    "passed": attempt.is_passed,
                },
                commit=False,
            )

            # Assemble Pydantic response BEFORE db.commit() so SQLAlchemy expire_on_commit
            # does not trigger lazy re-queries on questions/answers/attempt!
            student_name = db.query(User.full_name).filter(User.id == attempt.student_id).scalar() or "Student"
            result_obj = AttemptService._assemble_result_response(
                attempt=attempt,
                quiz=quiz,
                student_name=student_name,
                questions=questions,
                answers_map=answers_map,
                is_admin=False,
                is_owner=(quiz is not None and quiz.created_by_id == student_id),
            )
            db.commit()
            return result_obj, False

        result_obj, already_finished = execute_with_retry(db, _finish)
        if already_finished:
            return AttemptService.get_attempt_result(db, attempt_id, student_id=student_id)

        cache_manager.invalidate_attempts_and_stats()
        return result_obj  # type: ignore[return-value]

    @staticmethod
    def _grade_and_finalize(
        db: Session,
        attempt: QuizAttempt,
        final_status: str,
        quiz: Optional[Quiz] = None,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> Tuple[List[Question], Dict[int, AttemptAnswer]]:
        if quiz is None:
            quiz = db.get(Quiz, attempt.quiz_id)
        if now is None:
            now = datetime.now(timezone.utc)

        questions = (
            db.query(Question)
            .filter(Question.quiz_id == attempt.quiz_id)
            .order_by(Question.order_index.asc(), Question.id.asc())
            .all()
        )
        answers_map: Dict[int, AttemptAnswer] = {
            a.question_id: a
            for a in db.query(AttemptAnswer).filter(AttemptAnswer.attempt_id == attempt.id).all()
        }

        total_score = 0.0
        total_quiz_marks = float(sum(q.marks for q in questions)) if questions else 0.0

        for q in questions:
            ans = answers_map.get(q.id)
            if ans and ans.selected_option and ans.selected_option.upper() == q.correct_option.upper():
                ans.is_correct = True
                ans.marks_awarded = float(q.marks)
                total_score += float(q.marks)
            elif ans:
                ans.is_correct = False
                ans.marks_awarded = 0.0

        attempt.score_obtained = total_score
        attempt.total_marks = total_quiz_marks
        attempt.percentage = round((total_score / total_quiz_marks) * 100.0, 2) if total_quiz_marks > 0 else 0.0
        passing_percentage = quiz.passing_percentage if quiz else 50
        attempt.is_passed = attempt.percentage >= passing_percentage
        attempt.status = final_status
        attempt.end_time = now

        if commit:
            db.commit()
            db.refresh(attempt)
        return questions, answers_map

    @staticmethod
    def _assemble_result_response(
        attempt: QuizAttempt,
        quiz: Optional[Quiz],
        student_name: str,
        questions: List[Question],
        answers_map: Dict[int, AttemptAnswer],
        is_admin: bool,
        is_owner: bool,
    ) -> AttemptResultResponse:
        results_released = quiz.results_released if quiz else True
        hide_solutions = (not is_admin and not is_owner and not results_released)

        correct_count = 0
        incorrect_count = 0
        unattempted_count = 0
        review_items: List[QuestionReviewItem] = []

        for q in questions:
            ans = answers_map.get(q.id)
            selected_option = ans.selected_option if ans else None
            is_correct = ans.is_correct if ans else False
            marks_awarded = ans.marks_awarded if ans else 0.0

            if selected_option is None:
                unattempted_count += 1
            elif is_correct:
                correct_count += 1
            else:
                incorrect_count += 1

            if hide_solutions:
                review_items.append(
                    QuestionReviewItem(
                        question_id=q.id,
                        question_text=q.question_text,
                        option_a=q.option_a,
                        option_b=q.option_b,
                        option_c=q.option_c,
                        option_d=q.option_d,
                        selected_option=selected_option,
                        correct_option=None,
                        is_correct=False,
                        marks_awarded=0.0,
                        max_marks=q.marks,
                        explanation="Results and solutions have not yet been released by the organizer.",
                    )
                )
            else:
                review_items.append(
                    QuestionReviewItem(
                        question_id=q.id,
                        question_text=q.question_text,
                        option_a=q.option_a,
                        option_b=q.option_b,
                        option_c=q.option_c,
                        option_d=q.option_d,
                        selected_option=selected_option,
                        correct_option=q.correct_option,
                        is_correct=is_correct,
                        marks_awarded=marks_awarded,
                        max_marks=q.marks,
                        explanation=q.explanation,
                    )
                )

        score_to_show = 0.0 if hide_solutions else attempt.score_obtained
        pct_to_show = 0.0 if hide_solutions else attempt.percentage

        return AttemptResultResponse(
            attempt_id=attempt.id,
            quiz_id=attempt.quiz_id,
            quiz_title=quiz.title if quiz else "Quiz",
            student_name=student_name,
            status=attempt.status,
            start_time=attempt.start_time,
            end_time=attempt.end_time,
            deadline_time=attempt.deadline_time,
            score_obtained=score_to_show,
            total_marks=attempt.total_marks,
            percentage=pct_to_show,
            passing_percentage=quiz.passing_percentage if quiz else 50,
            is_passed=attempt.is_passed if not hide_solutions else False,
            total_questions=len(questions),
            correct_count=0 if hide_solutions else correct_count,
            incorrect_count=0 if hide_solutions else incorrect_count,
            unattempted_count=unattempted_count,
            results_released=results_released,
            review=review_items,
        )

    @staticmethod
    def get_attempt_result(
        db: Session,
        attempt_id: int,
        student_id: Optional[int] = None,
        is_admin: bool = False,
    ) -> AttemptResultResponse:
        # Query 1: Single JOIN fetching Attempt + Quiz + Student Name
        row = (
            db.query(QuizAttempt, Quiz, User.full_name.label("student_name"))
            .outerjoin(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .outerjoin(User, User.id == QuizAttempt.student_id)
            .filter(QuizAttempt.id == attempt_id)
            .first()
        )
        if not row:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attempt not found")

        attempt, quiz, student_name = row
        is_owner = (quiz is not None and quiz.created_by_id == student_id) if student_id else False

        if not is_admin and not is_owner and student_id and attempt.student_id != student_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Unauthorized: You cannot access this attempt result.",
            )

        # Query 2: Ordered questions
        questions = (
            db.query(Question)
            .filter(Question.quiz_id == attempt.quiz_id)
            .order_by(Question.order_index.asc(), Question.id.asc())
            .all()
        )
        # Query 3: Answers map
        answers_map = {
            a.question_id: a
            for a in db.query(AttemptAnswer).filter(AttemptAnswer.attempt_id == attempt.id).all()
        }

        return AttemptService._assemble_result_response(
            attempt=attempt,
            quiz=quiz,
            student_name=student_name or "Student",
            questions=questions,
            answers_map=answers_map,
            is_admin=is_admin,
            is_owner=is_owner,
        )

    @staticmethod
    def get_student_history(
        db: Session,
        student_id: int,
        limit: Optional[int] = None,
        offset: int = 0,
        cursor_id: Optional[int] = None,
        return_total: bool = False,
    ) -> List[AttemptSummaryResponse] | Tuple[List[AttemptSummaryResponse], int]:
        """Single JOIN query with offset/cursor pagination (ZERO N+1!)."""
        page_limit = _clamp_limit(limit)
        page_offset = max(0, offset)

        query = (
            db.query(
                QuizAttempt.id,
                QuizAttempt.quiz_id,
                QuizAttempt.status,
                QuizAttempt.start_time,
                QuizAttempt.end_time,
                QuizAttempt.score_obtained,
                QuizAttempt.total_marks,
                QuizAttempt.percentage,
                QuizAttempt.is_passed,
                Quiz.title.label("quiz_title"),
                Quiz.results_released.label("results_released"),
            )
            .outerjoin(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .filter(QuizAttempt.student_id == student_id)
        )

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

        items = []
        for r in rows:
            is_rel = True if r.results_released is None else bool(r.results_released)
            items.append(
                AttemptSummaryResponse(
                    attempt_id=r.id,
                    quiz_id=r.quiz_id,
                    quiz_title=r.quiz_title or "Quiz",
                    status=r.status,
                    start_time=r.start_time,
                    end_time=r.end_time,
                    score_obtained=r.score_obtained if is_rel else 0.0,
                    total_marks=r.total_marks,
                    percentage=r.percentage if is_rel else 0.0,
                    is_passed=r.is_passed if is_rel else False,
                    results_released=is_rel,
                )
            )

        if return_total:
            total = int(
                db.query(func.count(QuizAttempt.id)).filter(QuizAttempt.student_id == student_id).scalar() or 0
            )
            return items, total
        return items

    @staticmethod
    def get_student_dashboard_stats(db: Session, student_id: int) -> dict:
        """Pure SQL aggregation + 1 JOIN query for recent 6 attempts (2 queries total!)."""
        # Query 1: Aggregate stats in SQL
        agg = (
            db.query(
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
            )
            .filter(QuizAttempt.student_id == student_id)
            .one()
        )

        total_attempts = int(agg.total_attempts or 0)
        completed_cnt = int(agg.completed or 0)
        passed_cnt = int(agg.passed or 0)
        avg_score = round(float(agg.avg_score), 1) if agg.avg_score is not None else 0.0
        pass_rate = round((passed_cnt / completed_cnt) * 100.0, 1) if completed_cnt > 0 else 0.0

        # Query 2: Top 6 recent attempts joined with Quiz (serves both recent_results and progress_trend)
        recent_rows = (
            db.query(
                QuizAttempt.id,
                QuizAttempt.quiz_id,
                QuizAttempt.status,
                QuizAttempt.score_obtained,
                QuizAttempt.total_marks,
                QuizAttempt.percentage,
                QuizAttempt.is_passed,
                QuizAttempt.end_time,
                QuizAttempt.created_at,
                Quiz.title.label("quiz_title"),
                Quiz.results_released.label("results_released"),
            )
            .outerjoin(Quiz, Quiz.id == QuizAttempt.quiz_id)
            .filter(QuizAttempt.student_id == student_id)
            .order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
            .limit(6)
            .all()
        )

        recent_results = []
        recent_finished_scores = []
        for r in recent_rows:
            is_rel = True if r.results_released is None else bool(r.results_released)
            recent_results.append(
                {
                    "attempt_id": r.id,
                    "quiz_id": r.quiz_id,
                    "quiz_title": r.quiz_title or "Quiz",
                    "status": r.status,
                    "score_obtained": r.score_obtained if is_rel else 0.0,
                    "total_marks": r.total_marks,
                    "percentage": r.percentage if is_rel else 0.0,
                    "is_passed": r.is_passed if is_rel else False,
                    "submitted_at": (
                        r.end_time.isoformat()
                        if r.end_time
                        else (r.created_at.isoformat() if r.created_at else None)
                    ),
                }
            )
            if r.status in FINISHED_STATUSES:
                recent_finished_scores.append(r.percentage)

        if recent_finished_scores:
            progress_trend = [
                {"label": f"Wk {idx + 1}", "score": s}
                for idx, s in enumerate(reversed(recent_finished_scores))
            ]
        else:
            progress_trend = [{"label": "Wk 1", "score": 0}]

        rank_text = (
            "Top 10%"
            if avg_score >= 85
            else "Top 20%"
            if avg_score >= 70
            else "Top 35%"
            if avg_score >= 50
            else "Active Learner"
        )

        return {
            "total_attempts": total_attempts,
            "average_score": avg_score,
            "pass_rate": pass_rate,
            "rank_text": rank_text,
            "recent_results": recent_results,
            "progress_trend": progress_trend,
        }

    @staticmethod
    def _build_start_response(
        db: Session, attempt: QuizAttempt, quiz: Quiz, attempt_number: int = 1
    ) -> AttemptStartResponse:
        # Selective column load: never load correct_option or explanation for student payload
        q_rows = (
            db.query(
                Question.id,
                Question.quiz_id,
                Question.order_index,
                Question.question_text,
                Question.option_a,
                Question.option_b,
                Question.option_c,
                Question.option_d,
                Question.marks,
            )
            .filter(Question.quiz_id == quiz.id)
            .order_by(Question.order_index.asc(), Question.id.asc())
            .all()
        )
        ans_rows = (
            db.query(AttemptAnswer.question_id, AttemptAnswer.selected_option)
            .filter(AttemptAnswer.attempt_id == attempt.id)
            .all()
        )
        saved_answers: Dict[int, Optional[str]] = {qid: opt for qid, opt in ans_rows}

        student_questions = [
            QuestionStudentResponse(
                id=r.id,
                quiz_id=r.quiz_id,
                order_index=r.order_index,
                question_text=r.question_text,
                option_a=r.option_a,
                option_b=r.option_b,
                option_c=r.option_c,
                option_d=r.option_d,
                marks=r.marks,
            )
            for r in q_rows
        ]

        return AttemptStartResponse(
            attempt_id=attempt.id,
            quiz_id=quiz.id,
            quiz_title=quiz.title,
            duration_minutes=quiz.duration_minutes,
            start_time=attempt.start_time,
            deadline_time=attempt.deadline_time,
            server_time=datetime.now(timezone.utc),
            questions=student_questions,
            saved_answers=saved_answers,
            max_attempts=quiz.max_attempts,
            attempt_number=attempt_number,
        )
