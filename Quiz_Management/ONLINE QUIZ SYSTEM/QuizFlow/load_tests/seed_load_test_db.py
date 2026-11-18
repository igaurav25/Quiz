"""
Seeds an isolated load-testing database (never touches backend/quizflow.db)
with 1,200 students, 5 organizers, 1 admin, 15 published quizzes (10 questions each),
and pre-computed credentials for reproducible Locust & peak-submission tests.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
LOAD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKEND_DIR))

LOAD_DB_PATH = LOAD_DIR / "loadtest_quizflow.db"
CREDS_PATH = LOAD_DIR / "loadtest_credentials.json"


def seed_load_db(num_students: int = 1200, num_quizzes: int = 15) -> dict:
    db_url = os.getenv("LOADTEST_DATABASE_URL", f"sqlite:///{LOAD_DB_PATH}")
    if db_url.startswith("sqlite:///"):
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(LOAD_DB_PATH) + suffix)
            if p.exists():
                p.unlink()

    os.environ["DATABASE_URL"] = db_url
    os.environ["ENVIRONMENT"] = "development"
    os.environ["SECRET_KEY"] = os.getenv(
        "SECRET_KEY", "quizflow_loadtest_secret_key_at_least_32_characters_2026"
    )

    from app.core.security import create_access_token, get_password_hash
    from app.database.init_db import init_db
    from app.database.session import SessionLocal, engine
    from app.models.attempt import AttemptStatus, QuizAttempt
    from app.models.attempt_answer import AttemptAnswer
    from app.models.question import Question
    from app.models.quiz import Quiz
    from app.models.user import User, UserRole

    init_db()
    db = SessionLocal()
    try:
        # Hash password once for fast synthetic user seeding (all synthetic test accounts share test password)
        student_pw_hash = get_password_hash("LoadPass2026!")
        admin_pw_hash = get_password_hash("AdminLoad2026!")

        admin = User(
            email="load_admin@quizflow.local",
            full_name="Load Test Admin",
            hashed_password=admin_pw_hash,
            role=UserRole.ADMIN.value,
            is_active=True,
        )
        db.add(admin)

        organizers = [
            User(
                email=f"load_org{i}@quizflow.local",
                full_name=f"Load Organizer {i}",
                hashed_password=student_pw_hash,
                role=UserRole.ORGANIZER.value,
                is_active=True,
            )
            for i in range(5)
        ]
        db.add_all(organizers)

        students = [
            User(
                email=f"load_stu{i}@quizflow.local",
                full_name=f"Load Student {i}",
                hashed_password=student_pw_hash,
                role=UserRole.PARTICIPANT.value,
                is_active=True,
            )
            for i in range(num_students)
        ]
        db.add_all(students)
        db.flush()

        now = datetime.now(timezone.utc)
        quizzes = []
        for i in range(num_quizzes):
            qz = Quiz(
                title=f"Load Benchmark Exam {i + 1}",
                subject=["Computer Science", "Mathematics", "Science", "Systems"][i % 4],
                description=f"Standardized high-concurrency benchmark quiz #{i + 1}",
                instructions="Answer all questions before the timer expires.",
                duration_minutes=45,
                passing_percentage=50,
                total_marks=10,
                is_published=True,
                is_public=True,
                is_disabled=False,
                max_attempts=10,
                release_results_immediately=True,
                results_released=True,
                join_code=f"QF-L{i:03d}",
                created_by_id=organizers[i % len(organizers)].id,
                created_at=now - timedelta(days=i % 6),
            )
            quizzes.append(qz)
        db.add_all(quizzes)
        db.flush()

        all_questions = []
        quiz_questions = {}
        for qz in quizzes:
            q_list = []
            for j in range(10):
                q = Question(
                    quiz_id=qz.id,
                    question_text=f"Exam {qz.id} Question {j + 1}: Choose the optimal system architecture option.",
                    option_a="Horizontal stateless scaling with indexed queries",
                    option_b="Unbounded table scan inside loop",
                    option_c="Client-side score calculation",
                    option_d="Unpooled blocking synchronous I/O",
                    correct_option=["A", "B", "C", "D"][j % 4],
                    marks=1,
                    explanation="Indexed queries and stateless horizontal scaling ensure high concurrency.",
                    topic=f"Architecture Topic {(j % 3) + 1}",
                    difficulty=["easy", "medium", "hard"][j % 3],
                    order_index=j + 1,
                )
                q_list.append(q)
                all_questions.append(q)
            quiz_questions[qz.id] = q_list
        db.add_all(all_questions)
        db.flush()

        # Seed 100 initial historical attempts so admin/organizer dashboards have realistic data from t=0
        hist_attempts = []
        for idx in range(100):
            st = students[idx]
            qz = quizzes[idx % len(quizzes)]
            att = QuizAttempt(
                student_id=st.id,
                quiz_id=qz.id,
                start_time=now - timedelta(hours=3),
                deadline_time=now - timedelta(hours=2),
                end_time=now - timedelta(hours=2, minutes=40),
                status=AttemptStatus.COMPLETED.value,
                score_obtained=7.0,
                total_marks=10.0,
                percentage=70.0,
                is_passed=True,
                created_at=now - timedelta(days=idx % 6),
            )
            hist_attempts.append(att)
        db.add_all(hist_attempts)
        db.flush()

        hist_answers = []
        for att in hist_attempts:
            for j, q in enumerate(quiz_questions[att.quiz_id]):
                sel = q.correct_option if j < 7 else "D"
                hist_answers.append(
                    AttemptAnswer(
                        attempt_id=att.id,
                        question_id=q.id,
                        selected_option=sel,
                        is_correct=(sel == q.correct_option),
                        marks_awarded=1.0 if sel == q.correct_option else 0.0,
                    )
                )
        db.add_all(hist_answers)
        db.commit()

        # Pre-generate JWT tokens for realistic authenticated load + store login credentials
        creds = {
            "admin": {
                "email": admin.email,
                "password": "AdminLoad2026!",
                "token": create_access_token(str(admin.id), admin.role, admin.email, admin.full_name),
            },
            "organizers": [
                {
                    "id": o.id,
                    "email": o.email,
                    "password": "LoadPass2026!",
                    "token": create_access_token(str(o.id), o.role, o.email, o.full_name),
                }
                for o in organizers
            ],
            "students": [
                {
                    "id": s.id,
                    "email": s.email,
                    "password": "LoadPass2026!",
                    "token": create_access_token(str(s.id), s.role, s.email, s.full_name),
                }
                for s in students
            ],
            "quiz_ids": [qz.id for qz in quizzes],
        }
        CREDS_PATH.write_text(json.dumps(creds), encoding="utf-8")
        print(
            f"Seeded load-test DB ({db_url}): "
            f"{num_students} students, {len(organizers)} organizers, {len(quizzes)} quizzes, "
            f"{len(all_questions)} questions, {len(hist_attempts)} historical attempts."
        )
        return creds
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    seed_load_db()
