"""
High-Concurrency Quiz Attempt, Batch Answer Submission, Idempotent Finalization, and Scoring Endpoints.
"""
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, Header, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_student, get_current_user, get_db
from app.core.config import settings
from app.models.user import User, UserRole
from app.schemas.attempt import (
    AnswerSubmitRequest,
    AnswerSubmitResponse,
    AttemptResultResponse,
    AttemptStartResponse,
    AttemptSummaryResponse,
    BatchAnswerSubmitRequest,
    BatchAnswerSubmitResponse,
    FinishAttemptRequest,
    StudentDashboardStatsResponse,
)
from app.services.attempt_service import AttemptService

router = APIRouter()


@router.post(
    "/quizzes/{quiz_id}/start",
    response_model=AttemptStartResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Start or resume a quiz attempt (Student)",
)
def start_quiz_attempt(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_student: User = Depends(get_current_student),
):
    return AttemptService.start_quiz(db, quiz_id, current_student.id)


@router.post(
    "/{attempt_id}/answers",
    response_model=AnswerSubmitResponse,
    summary="Record or update an answer for a question (atomic UPSERT)",
)
def save_answer(
    attempt_id: int,
    ans_in: AnswerSubmitRequest,
    db: Session = Depends(get_db),
    current_student: User = Depends(get_current_student),
):
    return AttemptService.save_answer(db, attempt_id, current_student.id, ans_in)


@router.post(
    "/{attempt_id}/answers/batch",
    response_model=BatchAnswerSubmitResponse,
    summary="Atomically record or update a batch of answers in a single transaction",
)
def save_answers_batch(
    attempt_id: int,
    batch_in: BatchAnswerSubmitRequest,
    db: Session = Depends(get_db),
    current_student: User = Depends(get_current_student),
):
    return AttemptService.save_answers_batch(db, attempt_id, current_student.id, batch_in.answers)


@router.post(
    "/{attempt_id}/finish",
    response_model=AttemptResultResponse,
    summary="Finish and submit quiz attempt for atomic server-side grading",
)
def finish_attempt(
    attempt_id: int,
    payload: Optional[FinishAttemptRequest] = Body(default=None),
    idempotent: bool = Query(
        False, description="When true (or when Idempotency-Key header is set), returns finalized result on retry."
    ),
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    current_student: User = Depends(get_current_student),
):
    is_idempotent = idempotent or bool(idempotency_key)
    final_answers = payload.answers if payload else None
    return AttemptService.finish_quiz(
        db,
        attempt_id,
        current_student.id,
        final_answers=final_answers,
        idempotent=is_idempotent,
    )


@router.get(
    "/{attempt_id}/result",
    response_model=AttemptResultResponse,
    summary="Get result breakdown for a completed attempt",
)
def get_attempt_result(
    attempt_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    return AttemptService.get_attempt_result(
        db,
        attempt_id,
        student_id=current_user.id,
        is_admin=is_admin,
    )


@router.get(
    "/my-history",
    response_model=List[AttemptSummaryResponse],
    summary="Get current student's quiz attempt history with server-side pagination",
)
def get_my_history(
    response: Response,
    limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    cursor_id: Optional[int] = Query(None, ge=1),
    db: Session = Depends(get_db),
    current_student: User = Depends(get_current_student),
):
    items, total = AttemptService.get_student_history(
        db,
        current_student.id,
        limit=limit,
        offset=offset,
        cursor_id=cursor_id,
        return_total=True,
    )
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["X-Has-More"] = "true" if (offset + len(items)) < total else "false"
    if items:
        response.headers["X-Next-Cursor"] = str(items[-1].attempt_id)
    return items


@router.get(
    "/my-dashboard-stats",
    response_model=StudentDashboardStatsResponse,
    summary="Get summarized statistics and progress for student dashboard",
)
def get_student_dashboard_stats(
    db: Session = Depends(get_db),
    current_student: User = Depends(get_current_student),
):
    return AttemptService.get_student_dashboard_stats(db, current_student.id)
