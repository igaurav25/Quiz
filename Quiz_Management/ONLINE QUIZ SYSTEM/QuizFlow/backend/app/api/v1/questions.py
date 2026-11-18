"""
Question Management Endpoints for Organizers and Administrators.
Enforces strict quiz ownership checks and server-side pagination.
"""
from typing import List

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_organizer, get_db
from app.core.config import settings
from app.models.user import User, UserRole
from app.schemas.question import (
    QuestionAdminResponse,
    QuestionCreate,
    QuestionUpdate,
)
from app.services.quiz_service import QuizService

router = APIRouter()


@router.get(
    "/quizzes/{quiz_id}/questions",
    response_model=List[QuestionAdminResponse],
    summary="List questions for a quiz with pagination and ownership verification (Organizer or Admin)",
)
def get_quiz_questions(
    quiz_id: int,
    response: Response,
    limit: int = Query(settings.MAX_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    items, total = QuizService.get_quiz_questions_admin(
        db,
        quiz_id=quiz_id,
        user_id=current_user.id,
        is_admin=is_admin,
        limit=limit,
        offset=offset,
        return_total=True,
    )
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["X-Has-More"] = "true" if (offset + len(items)) < total else "false"
    return items


@router.post(
    "/quizzes/{quiz_id}/questions",
    response_model=QuestionAdminResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Add a question to a quiz (Organizer or Admin)",
)
def add_question(
    quiz_id: int,
    q_in: QuestionCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    return QuizService.add_question(db, quiz_id, q_in, user_id=current_user.id, is_admin=is_admin)


@router.put(
    "/questions/{question_id}",
    response_model=QuestionAdminResponse,
    summary="Update question details (Organizer or Admin)",
)
def update_question(
    question_id: int,
    q_in: QuestionUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    return QuizService.update_question(db, question_id, q_in, user_id=current_user.id, is_admin=is_admin)


@router.delete(
    "/questions/{question_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a question (Organizer or Admin)",
)
def delete_question(
    question_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    QuizService.delete_question(db, question_id, user_id=current_user.id, is_admin=is_admin)
    return {"message": "Question deleted successfully", "question_id": question_id}
