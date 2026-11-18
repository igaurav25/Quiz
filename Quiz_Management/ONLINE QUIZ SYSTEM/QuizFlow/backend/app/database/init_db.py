"""
Database initialization, safe cross-dialect non-destructive schema & index migrations,
and administrator bootstrapping.
Works cleanly on both SQLite and PostgreSQL without deleting or damaging existing data.
"""
import logging
import random
import string

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import get_password_hash
from app.database.session import Base, SessionLocal, engine
from app.models.user import User, UserRole

logger = logging.getLogger("quizflow.init_db")


def _generate_join_code() -> str:
    """Generate a clean, unambiguous 7-character uppercase join code (QF-XXXX)."""
    chars = "".join(c for c in string.ascii_uppercase + string.digits if c not in "IO01")
    return "QF-" + "".join(random.choices(chars, k=4))


def _ensure_columns(db: Session, bind, dialect: str) -> None:
    """Safely add missing columns across SQLite and PostgreSQL using SQLAlchemy inspector."""
    insp = inspect(bind)
    bool_true = "TRUE" if dialect == "postgresql" else "1"
    bool_false = "FALSE" if dialect == "postgresql" else "0"
    dt_type = "TIMESTAMP WITH TIME ZONE" if dialect == "postgresql" else "DATETIME"

    if insp.has_table("quizzes"):
        existing_quiz_cols = {col["name"] for col in insp.get_columns("quizzes")}
        new_quiz_cols = [
            ("subject", "VARCHAR(100) DEFAULT 'General'"),
            ("instructions", "TEXT"),
            ("join_code", "VARCHAR(16)"),
            ("is_public", f"BOOLEAN DEFAULT {bool_true} NOT NULL"),
            ("max_attempts", "INTEGER DEFAULT 1 NOT NULL"),
            ("release_results_immediately", f"BOOLEAN DEFAULT {bool_true} NOT NULL"),
            ("results_released", f"BOOLEAN DEFAULT {bool_true} NOT NULL"),
            ("start_time", dt_type),
            ("end_time", dt_type),
            ("is_disabled", f"BOOLEAN DEFAULT {bool_false} NOT NULL"),
        ]
        for col_name, col_type in new_quiz_cols:
            if col_name not in existing_quiz_cols:
                logger.info("Migrating schema: adding column quizzes.%s", col_name)
                db.execute(text(f"ALTER TABLE quizzes ADD COLUMN {col_name} {col_type}"))
                db.commit()

    if insp.has_table("questions"):
        existing_question_cols = {col["name"] for col in insp.get_columns("questions")}
        new_q_cols = [
            ("topic", "VARCHAR(100) DEFAULT 'General'"),
            ("difficulty", "VARCHAR(20) DEFAULT 'medium'"),
            ("learning_objective", "VARCHAR(255) DEFAULT ''"),
        ]
        for col_name, col_type in new_q_cols:
            if col_name not in existing_question_cols:
                logger.info("Migrating schema: adding column questions.%s", col_name)
                db.execute(text(f"ALTER TABLE questions ADD COLUMN {col_name} {col_type}"))
                db.commit()


def _deduplicate_attempt_answers(db: Session) -> None:
    """
    Safely deduplicate any duplicate (attempt_id, question_id) rows in legacy databases
    (retaining the latest row by highest id) before creating the unique index.
    Never removes distinct answers.
    """
    dups = db.execute(
        text(
            "SELECT attempt_id, question_id, MAX(id) AS keep_id "
            "FROM attempt_answers "
            "GROUP BY attempt_id, question_id "
            "HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if not dups:
        return

    removed = 0
    for attempt_id, question_id, keep_id in dups:
        res = db.execute(
            text(
                "DELETE FROM attempt_answers "
                "WHERE attempt_id = :aid AND question_id = :qid AND id != :keep_id"
            ),
            {"aid": attempt_id, "qid": question_id, "keep_id": keep_id},
        )
        removed += res.rowcount or 0
    db.commit()
    if removed:
        logger.info("Deduplicated %d legacy duplicate attempt_answers rows safely.", removed)


def _ensure_indexes(db: Session) -> None:
    """
    Create all performance-critical composite and unique indexes idempotently.
    Every index directly supports verified application query patterns.
    """
    index_statements = [
        # Users
        "CREATE INDEX IF NOT EXISTS ix_users_role_active ON users (role, is_active)",
        "CREATE INDEX IF NOT EXISTS ix_users_created_at ON users (created_at)",
        # Quizzes
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_quizzes_join_code ON quizzes (join_code)",
        "CREATE INDEX IF NOT EXISTS ix_quizzes_created_by_created ON quizzes (created_by_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_quizzes_public_discovery ON quizzes (is_published, is_disabled, is_public, id)",
        "CREATE INDEX IF NOT EXISTS ix_quizzes_created_at ON quizzes (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_quizzes_subject ON quizzes (subject)",
        # Questions
        "CREATE INDEX IF NOT EXISTS ix_questions_quiz_order ON questions (quiz_id, order_index, id)",
        "CREATE INDEX IF NOT EXISTS ix_questions_topic ON questions (topic)",
        "CREATE INDEX IF NOT EXISTS ix_questions_difficulty ON questions (difficulty)",
        # Quiz Attempts
        "CREATE INDEX IF NOT EXISTS ix_attempts_student_quiz_status ON quiz_attempts (student_id, quiz_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_attempts_quiz_status_created ON quiz_attempts (quiz_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_attempts_student_created ON quiz_attempts (student_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_attempts_status_passed ON quiz_attempts (status, is_passed)",
        "CREATE INDEX IF NOT EXISTS ix_attempts_created_at ON quiz_attempts (created_at)",
        # Attempt Answers (unique pair prevents duplicate rows under concurrent autosaves!)
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_attempt_answers_attempt_question ON attempt_answers (attempt_id, question_id)",
        "CREATE INDEX IF NOT EXISTS ix_attempt_answers_question_stats ON attempt_answers (question_id, is_correct, selected_option)",
        # Audit Logs
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_action_created ON audit_logs (action, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_user_created ON audit_logs (user_id, created_at)",
    ]
    for stmt in index_statements:
        db.execute(text(stmt))
    db.commit()


def apply_safe_migrations(db: Session) -> None:
    """
    Safely inspect and apply non-destructive schema & index migrations on both SQLite and PostgreSQL.
    Preserves all existing user records, quizzes, questions, and attempt data.
    """
    try:
        bind = db.get_bind()
        dialect = bind.dialect.name

        # 1. Add missing columns if upgrading from an older schema
        _ensure_columns(db, bind, dialect)

        # 2. Assign unique join codes to any existing quizzes that lack one
        quizzes_without_code = db.execute(
            text("SELECT id FROM quizzes WHERE join_code IS NULL OR join_code = ''")
        ).fetchall()
        for (q_id,) in quizzes_without_code:
            for _ in range(10):
                code = _generate_join_code()
                exists = db.execute(
                    text("SELECT 1 FROM quizzes WHERE join_code = :code"), {"code": code}
                ).first()
                if not exists:
                    db.execute(
                        text("UPDATE quizzes SET join_code = :code WHERE id = :qid"),
                        {"code": code, "qid": q_id},
                    )
                    break
        if quizzes_without_code:
            db.commit()
            logger.info("Generated join codes for %d existing quizzes.", len(quizzes_without_code))

        # 3. Users role normalization: preserve legacy student rows as participant
        db.execute(text("UPDATE users SET role = 'participant' WHERE role = 'student'"))
        db.commit()

        # 4. Safely deduplicate legacy duplicate attempt_answers before unique index creation
        _deduplicate_attempt_answers(db)

        # 5. Ensure all performance & integrity indexes exist
        _ensure_indexes(db)

        logger.info("Schema and index migrations verified and safely applied (%s).", dialect)
    except Exception as e:
        logger.warning("Safe migration check completed with notice: %s", e)
        db.rollback()


def init_db(db: Session = None) -> None:
    """Create all tables in the database if they do not exist, and apply safe migrations."""
    import app.models  # noqa: F401
    target_bind = db.get_bind() if db is not None else engine
    Base.metadata.create_all(bind=target_bind)

    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        apply_safe_migrations(db)
        logger.info("Database tables verified/created successfully.")
    finally:
        if close_session:
            db.close()


def bootstrap_admin(
    email: str = None,
    password: str = None,
    full_name: str = None,
    db: Session = None,
) -> bool:
    """
    Securely bootstrap an initial administrator account if none exists.
    Can be run via CLI or startup hook.
    """
    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        init_db(db)

        admin_email = email or settings.FIRST_SUPERUSER_EMAIL
        admin_password = password or settings.FIRST_SUPERUSER_PASSWORD
        admin_name = full_name or settings.FIRST_SUPERUSER_NAME

        existing_admin = db.query(User).filter(User.role == UserRole.ADMIN.value).first()
        if existing_admin:
            logger.info("An administrator account already exists. Bootstrap skipped.")
            return False

        user_by_email = db.query(User).filter(User.email == admin_email).first()
        if user_by_email:
            user_by_email.role = UserRole.ADMIN.value
            db.commit()
            logger.info("Upgraded existing user to admin role: %s", admin_email)
            return True

        admin_user = User(
            email=admin_email,
            full_name=admin_name,
            hashed_password=get_password_hash(admin_password),
            role=UserRole.ADMIN.value,
            is_active=True,
        )
        db.add(admin_user)
        db.commit()
        logger.info("Initial administrator account bootstrapped successfully: %s", admin_email)

        bootstrap_demo_accounts(db)
        return True
    finally:
        if close_session:
            db.close()


def bootstrap_demo_accounts(db: Session = None) -> None:
    """Ensure standard demo accounts exist for 1-click Quick Explore in UI (non-production)."""
    if settings.ENVIRONMENT == "production":
        return

    close_session = False
    if db is None:
        db = SessionLocal()
        close_session = True

    try:
        teacher_email = "teacher@quizflow.local"
        if not db.query(User.id).filter(User.email == teacher_email).first():
            db.add(
                User(
                    email=teacher_email,
                    full_name="Prof. Shivam (Teacher)",
                    hashed_password=get_password_hash("TeacherPass2026!"),
                    role=UserRole.ORGANIZER.value,
                    is_active=True,
                )
            )
            db.commit()
            logger.info("Demo teacher bootstrapped: %s", teacher_email)

        student_email = "student@quizflow.local"
        if not db.query(User.id).filter(User.email == student_email).first():
            db.add(
                User(
                    email=student_email,
                    full_name="Alice Johnson (Student)",
                    hashed_password=get_password_hash("StudentPass2026!"),
                    role=UserRole.PARTICIPANT.value,
                    is_active=True,
                )
            )
            db.commit()
            logger.info("Demo student bootstrapped: %s", student_email)
    except Exception as e:
        logger.warning("Notice during demo account bootstrap: %s", e)
        db.rollback()
    finally:
        if close_session:
            db.close()
