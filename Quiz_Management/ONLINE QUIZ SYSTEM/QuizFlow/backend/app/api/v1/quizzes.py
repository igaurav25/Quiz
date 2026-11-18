"""
Quiz Management Endpoints for Participants, Organizers, and Administrators.
Enforces server-side pagination, direct PK lookups, and cache-backed public listings.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_admin,
    get_current_organizer,
    get_current_user,
    get_db,
    get_optional_current_user,
)
from app.core.config import settings
from app.models.user import User, UserRole
from app.schemas.quiz import (
    JoinCodeRequest,
    QuizAdminResponse,
    QuizCreate,
    QuizDetailStudentResponse,
    QuizStudentResponse,
    QuizUpdate,
)
from app.services.quiz_service import QuizService

router = APIRouter()


def _set_pagination_headers(response: Response, total: int, limit: int, offset: int, count: int) -> None:
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["X-Has-More"] = "true" if (offset + count) < total else "false"


@router.get(
    "",
    response_model=List[QuizStudentResponse],
    summary="List public published quizzes with server-side pagination",
)
def list_published_quizzes(
    response: Response,
    limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    subject: Optional[str] = Query(None, max_length=100),
    search: Optional[str] = Query(None, max_length=100),
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    user_id = current_user.id if current_user else None
    items, total = QuizService.get_student_quizzes(
        db,
        student_id=user_id,
        limit=limit,
        offset=offset,
        subject=subject,
        search=search,
        return_total=True,
    )
    _set_pagination_headers(response, total, limit, offset, len(items))
    return items


@router.post(
    "/join-by-code",
    response_model=QuizStudentResponse,
    summary="Discover and join a private or public quiz using an Organizer's Join Code",
)
def join_quiz_by_code(
    payload: JoinCodeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return QuizService.get_quiz_by_join_code(db, payload.join_code, student_id=current_user.id)


@router.get(
    "/by-code/{join_code}",
    response_model=QuizStudentResponse,
    summary="Get quiz details by join code",
)
def get_quiz_by_code(
    join_code: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return QuizService.get_quiz_by_join_code(db, join_code, student_id=current_user.id)


@router.get(
    "/admin/all",
    response_model=List[QuizAdminResponse],
    summary="List all platform quizzes with server-side pagination (Admin only)",
)
def list_admin_quizzes(
    response: Response,
    limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    search: Optional[str] = Query(None, max_length=100),
    db: Session = Depends(get_db),
    current_admin: User = Depends(get_current_admin),
):
    items, total = QuizService.get_admin_quizzes(
        db, limit=limit, offset=offset, search=search, return_total=True
    )
    _set_pagination_headers(response, total, limit, offset, len(items))
    return items


@router.post(
    "",
    response_model=QuizAdminResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new quiz (Organizer or Admin)",
)
def create_quiz(
    quiz_in: QuizCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    return QuizService.create_quiz(db, quiz_in, creator_id=current_user.id)


@router.get(
    "/{quiz_id}",
    response_model=QuizDetailStudentResponse,
    summary="Get quiz instructions/details",
)
def get_quiz_details(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user),
):
    return QuizService.get_quiz_details(db, quiz_id=quiz_id, current_user=current_user)


@router.put(
    "/{quiz_id}",
    response_model=QuizAdminResponse,
    summary="Update quiz settings (Organizer or Admin)",
)
def update_quiz(
    quiz_id: int,
    quiz_in: QuizUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    return QuizService.update_quiz(db, quiz_id, quiz_in, user_id=current_user.id, is_admin=is_admin)


@router.delete(
    "/{quiz_id}",
    status_code=status.HTTP_200_OK,
    summary="Delete a quiz (Organizer or Admin)",
)
def delete_quiz(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    QuizService.delete_quiz(db, quiz_id, user_id=current_user.id, is_admin=is_admin)
    return {"message": "Quiz deleted successfully", "quiz_id": quiz_id}


@router.post(
    "/{quiz_id}/publish",
    response_model=QuizAdminResponse,
    summary="Publish a quiz (Organizer or Admin)",
)
def publish_quiz(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    return QuizService.publish_quiz(db, quiz_id, user_id=current_user.id, is_admin=is_admin)


@router.post(
    "/{quiz_id}/unpublish",
    response_model=QuizAdminResponse,
    summary="Unpublish a quiz (Organizer or Admin)",
)
def unpublish_quiz(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_organizer),
):
    is_admin = current_user.role == UserRole.ADMIN.value
    return QuizService.unpublish_quiz(db, quiz_id, user_id=current_user.id, is_admin=is_admin)
