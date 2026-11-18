"""
Quiz Attempt Model for QuizFlow.
Manages student attempt lifecycles, timestamps, deadline enforcement, and scoring.
"""
import enum
from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import relationship

from app.database.session import Base


class AttemptStatus(str, enum.Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    EXPIRED = "expired"


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    __table_args__ = (
        Index("ix_attempts_student_quiz_status", "student_id", "quiz_id", "status"),
        Index("ix_attempts_quiz_status_created", "quiz_id", "status", "created_at"),
        Index("ix_attempts_student_created", "student_id", "created_at"),
        Index("ix_attempts_status_passed", "status", "is_passed"),
        Index("ix_attempts_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    student_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    quiz_id = Column(Integer, ForeignKey("quizzes.id", ondelete="CASCADE"), nullable=False, index=True)
    start_time = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)
    deadline_time = Column(DateTime(timezone=True), nullable=False)
    end_time = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), default=AttemptStatus.IN_PROGRESS.value, nullable=False, index=True)
    score_obtained = Column(Float, default=0.0, nullable=False)
    total_marks = Column(Float, default=0.0, nullable=False)
    percentage = Column(Float, default=0.0, nullable=False)
    is_passed = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

    # Relationships
    student = relationship("User", back_populates="attempts")
    quiz = relationship("Quiz", back_populates="attempts")
    answers = relationship("AttemptAnswer", back_populates="attempt", cascade="all, delete-orphan")

    def __repr__(self) -> str:
        return f"<QuizAttempt id={self.id} student_id={self.student_id} status='{self.status}'>"
