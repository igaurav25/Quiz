"""
Organizer (Teacher) Workspace Endpoints.
Provides personal dashboard statistics, paginated quiz management, participant grading,
synchronous and asynchronous background CSV exports, and AI quiz generation.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_current_organizer, get_db
from app.core.cache import cache_manager
from app.core.config import settings
from app.models.attempt import QuizAttempt
from app.models.quiz import Quiz
from app.models.user import User, UserRole
from app.schemas.admin import ResultListItem
from app.schemas.quiz import (
    AIGeneratedQuizResponse,
    AIGenerateQuizRequest,
    AIImportFileQuizRequest,
    OrganizerDashboardStats,
    QuizAdminResponse,
    QuizReportResponse,
)
from app.services.ai_generator_service import AIGeneratorService
from app.services.quiz_service import QuizService
from app.workers.tasks import enqueue_csv_export, get_job_state

router = APIRouter()


@router.get(
    "/dashboard-stats",
    response_model=OrganizerDashboardStats,
    summary="Get statistics for the logged-in organizer",
)
def get_organizer_stats(
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    return QuizService.get_organizer_dashboard_stats(db, organizer_id=current_organizer.id)


@router.get(
    "/quizzes",
    response_model=List[QuizAdminResponse],
    summary="List quizzes owned by the logged-in organizer with server-side pagination",
)
def get_my_quizzes(
    response: Response,
    limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    items, total = QuizService.get_organizer_quizzes(
        db, organizer_id=current_organizer.id, limit=limit, offset=offset, return_total=True
    )
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["X-Has-More"] = "true" if (offset + len(items)) < total else "false"
    return items


@router.get(
    "/results",
    response_model=List[ResultListItem],
    summary="List paginated student submissions across all quizzes owned by this organizer",
)
def get_organizer_results(
    response: Response,
    limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    is_admin = current_organizer.role == UserRole.ADMIN.value
    base_q = (
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
        .join(Quiz, Quiz.id == QuizAttempt.quiz_id)
        .outerjoin(User, User.id == QuizAttempt.student_id)
    )
    cnt_q = db.query(func.count(QuizAttempt.id)).join(Quiz, Quiz.id == QuizAttempt.quiz_id)

    if not is_admin:
        base_q = base_q.filter(Quiz.created_by_id == current_organizer.id)
        cnt_q = cnt_q.filter(Quiz.created_by_id == current_organizer.id)

    rows = (
        base_q.order_by(QuizAttempt.created_at.desc(), QuizAttempt.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    total = int(cnt_q.scalar() or 0)

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
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Limit"] = str(limit)
    response.headers["X-Offset"] = str(offset)
    response.headers["X-Has-More"] = "true" if (offset + len(items)) < total else "false"
    return items


@router.get(
    "/quizzes/{quiz_id}/report",
    response_model=QuizReportResponse,
    summary="Get comprehensive attempt report for an owned quiz (paginated attempts)",
)
def get_quiz_report(
    quiz_id: int,
    attempts_limit: int = Query(settings.DEFAULT_PAGE_SIZE, ge=1, le=settings.MAX_PAGE_SIZE),
    attempts_offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    is_admin = current_organizer.role == UserRole.ADMIN.value
    return QuizService.get_quiz_report(
        db,
        quiz_id,
        user_id=current_organizer.id,
        is_admin=is_admin,
        attempts_limit=attempts_limit,
        attempts_offset=attempts_offset,
    )


@router.get(
    "/quizzes/{quiz_id}/export-csv",
    summary="Export quiz attempt results as CSV",
)
def export_quiz_results_csv(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    is_admin = current_organizer.role == UserRole.ADMIN.value
    csv_data = QuizService.export_quiz_csv(db, quiz_id, user_id=current_organizer.id, is_admin=is_admin)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=quizflow_results_quiz_{quiz_id}.csv"},
    )


@router.post(
    "/quizzes/{quiz_id}/export-csv/async",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Queue a non-blocking background CSV report export job",
)
def enqueue_quiz_results_csv_export(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    quiz = db.get(Quiz, quiz_id)
    if not quiz:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Quiz not found")
    is_admin = current_organizer.role == UserRole.ADMIN.value
    if not is_admin and quiz.created_by_id != current_organizer.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied to another organizer's quiz.")

    return enqueue_csv_export(quiz_id=quiz_id, user_id=current_organizer.id, is_admin=is_admin)


@router.get(
    "/tasks/{task_id}",
    summary="Check status/result of a background export task",
)
def get_organizer_task_status(
    task_id: str,
    current_organizer: User = Depends(get_current_organizer),
):
    is_admin = current_organizer.role == UserRole.ADMIN.value
    state = get_job_state(task_id, owner_user_id=current_organizer.id, is_admin=is_admin)
    if not state:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return state


@router.post(
    "/quizzes/{quiz_id}/release-results",
    response_model=QuizAdminResponse,
    summary="Release quiz results to participants",
)
def release_quiz_results(
    quiz_id: int,
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    is_admin = current_organizer.role == UserRole.ADMIN.value
    return QuizService.release_results(db, quiz_id, user_id=current_organizer.id, is_admin=is_admin)


@router.post(
    "/ai/generate-quiz",
    response_model=AIGeneratedQuizResponse,
    summary="Generate assessment quiz using AI prompt",
)
def generate_quiz_with_ai(
    req: AIGenerateQuizRequest,
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    generated = AIGeneratorService.generate_quiz_from_prompt(
        prompt=req.prompt,
        num_questions=req.num_questions,
        difficulty=req.difficulty,
        topic=req.topic,
    )

    quiz_id = None
    join_code = None
    if req.save_directly:
        saved_quiz = AIGeneratorService.save_quiz_to_database(
            db=db,
            quiz_data=generated,
            creator_id=current_organizer.id,
            is_public=True,
        )
        quiz_id = saved_quiz.id
        join_code = saved_quiz.join_code
        cache_manager.invalidate_quizzes(quiz_id=quiz_id)

    return AIGeneratedQuizResponse(
        title=generated["title"],
        description=generated.get("description"),
        subject=generated.get("subject", "General"),
        duration_minutes=generated.get("duration_minutes", 30),
        passing_percentage=generated.get("passing_percentage", 50),
        questions=generated["questions"],
        quiz_id=quiz_id,
        join_code=join_code,
        source=generated.get("source", "ai_generated"),
    )


@router.post(
    "/ai/import-file-quiz",
    response_model=AIGeneratedQuizResponse,
    summary="Create quiz automatically from an uploaded file (CSV, JSON, TXT, MD)",
)
def import_quiz_from_file(
    req: AIImportFileQuizRequest,
    db: Session = Depends(get_db),
    current_organizer: User = Depends(get_current_organizer),
):
    parsed = AIGeneratorService.parse_and_generate_from_file(
        file_content=req.file_content,
        filename=req.filename,
        num_questions=req.num_questions,
        difficulty=req.difficulty,
    )

    quiz_id = None
    join_code = None
    if req.save_directly:
        saved_quiz = AIGeneratorService.save_quiz_to_database(
            db=db,
            quiz_data=parsed,
            creator_id=current_organizer.id,
            is_public=True,
        )
        quiz_id = saved_quiz.id
        join_code = saved_quiz.join_code
        cache_manager.invalidate_quizzes(quiz_id=quiz_id)

    return AIGeneratedQuizResponse(
        title=parsed["title"],
        description=parsed.get("description"),
        subject=parsed.get("subject", "Imported"),
        duration_minutes=parsed.get("duration_minutes", 30),
        passing_percentage=parsed.get("passing_percentage", 50),
        questions=parsed["questions"],
        quiz_id=quiz_id,
        join_code=join_code,
        source=parsed.get("source", "file_imported"),
    )
