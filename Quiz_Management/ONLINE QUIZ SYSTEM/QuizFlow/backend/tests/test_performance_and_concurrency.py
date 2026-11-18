"""
Comprehensive Automated Tests for Performance, Zero-N+1 Query Counts, Pagination,
High-Concurrency Quiz Attempts, Idempotency, Caching, Rate Limiting, Monitoring,
Background Tasks, Authorization, and Safe Migrations.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import time

from sqlalchemy import event, text

from app.core.cache import BoundedTTLCache, cache_manager
from app.core.config import settings
from app.core.rate_limit import rate_limiter
from app.core.security import create_access_token
from app.database.init_db import apply_safe_migrations
from app.models.attempt import AttemptStatus, QuizAttempt
from app.models.attempt_answer import AttemptAnswer
from app.models.audit_log import AuditLog
from app.models.question import Question
from app.models.quiz import Quiz
from app.models.user import User, UserRole
from app.services.admin_service import AdminService
from app.services.attempt_service import AttemptService
from app.services.quiz_service import QuizService


def _seed_medium_dataset(db):
    """Seed 20 students, 5 organizers, 15 quizzes, 75 questions, 40 attempts, 200 answers, 30 logs."""
    now = datetime.now(timezone.utc)
    organizers = [
        User(
            email=f"perf_org{i}@test.local",
            full_name=f"Perf Organizer {i}",
            hashed_password="h",
            role=UserRole.ORGANIZER.value,
            is_active=True,
        )
        for i in range(5)
    ]
    students = [
        User(
            email=f"perf_stu{i}@test.local",
            full_name=f"Perf Student {i}",
            hashed_password="h",
            role=UserRole.PARTICIPANT.value,
            is_active=True,
        )
        for i in range(20)
    ]
    db.add_all(organizers + students)
    db.flush()

    quizzes = []
    for i in range(15):
        qz = Quiz(
            title=f"Perf Quiz {i}",
            subject="Computer Science" if i % 2 == 0 else "Mathematics",
            description="Performance test quiz",
            duration_minutes=20,
            passing_percentage=50,
            total_marks=5,
            is_published=True,
            is_public=True,
            is_disabled=False,
            max_attempts=5,
            join_code=f"QF-P{i:03d}",
            created_by_id=organizers[i % len(organizers)].id,
            created_at=now - timedelta(days=i % 5),
        )
        quizzes.append(qz)
    db.add_all(quizzes)
    db.flush()

    questions_by_quiz = {}
    for qz in quizzes:
        qs = [
            Question(
                quiz_id=qz.id,
                question_text=f"Q{j} for quiz {qz.id}?",
                option_a="A",
                option_b="B",
                option_c="C",
                option_d="D",
                correct_option="A",
                marks=1,
                topic=f"Topic {j % 2}",
                difficulty="medium",
                order_index=j,
            )
            for j in range(5)
        ]
        db.add_all(qs)
        questions_by_quiz[qz.id] = qs
    db.flush()

    attempts = []
    for idx in range(40):
        stu = students[idx % len(students)]
        qz = quizzes[idx % len(quizzes)]
        att = QuizAttempt(
            student_id=stu.id,
            quiz_id=qz.id,
            start_time=now - timedelta(hours=idx + 1),
            deadline_time=now - timedelta(hours=idx) + timedelta(minutes=20),
            end_time=now - timedelta(hours=idx),
            status=AttemptStatus.COMPLETED.value,
            score_obtained=4.0,
            total_marks=5.0,
            percentage=80.0,
            is_passed=True,
            created_at=now - timedelta(hours=idx + 1),
        )
        attempts.append(att)
    db.add_all(attempts)
    db.flush()

    answers = []
    for att in attempts:
        for j, q in enumerate(questions_by_quiz[att.quiz_id]):
            sel = "A" if j < 4 else "B"
            answers.append(
                AttemptAnswer(
                    attempt_id=att.id,
                    question_id=q.id,
                    selected_option=sel,
                    is_correct=(sel == "A"),
                    marks_awarded=1.0 if sel == "A" else 0.0,
                )
            )
    db.add_all(answers)

    logs = [
        AuditLog(user_id=students[i % len(students)].id, action="AUTH_LOGIN_SUCCESS", resource_type="user")
        for i in range(30)
    ]
    db.add_all(logs)
    db.commit()
    return organizers[0], students[0], quizzes[0]


def test_zero_n_plus_one_query_counts(db_session):
    """Verify every core listing/dashboard/report service runs in O(1) SQL queries (zero N+1)."""
    org0, stu0, quiz0 = _seed_medium_dataset(db_session)
    bind = db_session.get_bind()
    counter = {"n": 0}

    def _count(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(bind, "before_cursor_execute", _count)
    try:
        def assert_max_queries(label, max_q, fn):
            counter["n"] = 0
            res = fn()
            assert counter["n"] <= max_q, f"{label} executed {counter['n']} queries (expected <= {max_q})"
            return res

        assert_max_queries("get_dashboard_stats", 8, lambda: AdminService.get_dashboard_stats(db_session))
        assert_max_queries("get_users_list", 2, lambda: AdminService.get_users_list(db_session, limit=50))
        assert_max_queries("get_audit_logs", 2, lambda: AdminService.get_audit_logs(db_session, limit=30))
        assert_max_queries("get_platform_reports", 4, lambda: AdminService.get_platform_reports(db_session))
        assert_max_queries("get_all_results", 2, lambda: AdminService.get_all_results(db_session, limit=50))
        assert_max_queries("get_admin_quizzes", 2, lambda: QuizService.get_admin_quizzes(db_session, limit=50))
        assert_max_queries("get_organizer_quizzes", 2, lambda: QuizService.get_organizer_quizzes(db_session, org0.id, limit=50))
        assert_max_queries("get_organizer_dashboard_stats", 3, lambda: QuizService.get_organizer_dashboard_stats(db_session, org0.id))
        assert_max_queries("get_student_quizzes", 3, lambda: QuizService.get_student_quizzes(db_session, stu0.id, limit=50))
        assert_max_queries("get_quiz_details", 2, lambda: QuizService.get_quiz_details(db_session, quiz0.id))
        assert_max_queries("get_quiz_report", 5, lambda: QuizService.get_quiz_report(db_session, quiz0.id, org0.id, is_admin=True))
        assert_max_queries("get_student_history", 2, lambda: AttemptService.get_student_history(db_session, stu0.id, limit=50))
        assert_max_queries("get_student_dashboard_stats", 3, lambda: AttemptService.get_student_dashboard_stats(db_session, stu0.id))
    finally:
        event.remove(bind, "before_cursor_execute", _count)


def test_server_side_pagination_and_headers(client, db_session, admin_token):
    """Verify limit, offset, cursor_id, X-Total-Count, X-Has-More, and max-limit enforcement."""
    org0, stu0, quiz0 = _seed_medium_dataset(db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}

    # 1. Paginated public quizzes
    r1 = client.get("/api/v1/quizzes?limit=5&offset=0")
    assert r1.status_code == 200
    assert len(r1.json()) == 5
    assert int(r1.headers["X-Total-Count"]) == 15
    assert r1.headers["X-Has-More"] == "true"

    r2 = client.get("/api/v1/quizzes?limit=5&offset=10")
    assert r2.status_code == 200
    assert len(r2.json()) == 5
    assert r2.headers["X-Has-More"] == "false"

    # 2. Enforce max limit rejection (limit > 200 -> 422)
    r_bad = client.get("/api/v1/quizzes?limit=999")
    assert r_bad.status_code == 422

    # 3. Admin users pagination
    u1 = client.get("/api/v1/admin/users?limit=10&offset=0", headers=headers)
    assert u1.status_code == 200
    assert len(u1.json()) == 10
    assert int(u1.headers["X-Total-Count"]) >= 25
    assert u1.headers["X-Has-More"] == "true"

    # 4. Admin results cursor pagination
    res_p1 = client.get("/api/v1/admin/results?limit=10", headers=headers)
    assert res_p1.status_code == 200
    p1_data = res_p1.json()
    assert len(p1_data) == 10
    next_cursor = int(res_p1.headers["X-Next-Cursor"])

    res_p2 = client.get(f"/api/v1/admin/results?limit=10&cursor_id={next_cursor}", headers=headers)
    assert res_p2.status_code == 200
    p2_data = res_p2.json()
    assert len(p2_data) == 10
    # Strict keyset ordering: no overlapping IDs between pages
    assert set(x["attempt_id"] for x in p1_data).isdisjoint(x["attempt_id"] for x in p2_data)

    # 5. Paginated quiz report attempts
    rep = client.get(f"/api/v1/organizer/quizzes/{quiz0.id}/report?attempts_limit=2&attempts_offset=0", headers=headers)
    assert rep.status_code == 200
    rep_data = rep.json()
    assert len(rep_data["attempts"]) <= 2
    assert rep_data["attempts_limit"] == 2
    assert rep_data["total_attempts"] >= len(rep_data["attempts"])

    # 6. Student history pagination
    stu_tok = create_access_token(subject=str(stu0.id), role=stu0.role, email=stu0.email, name=stu0.full_name)
    h1 = client.get("/api/v1/attempts/my-history?limit=1&offset=0", headers={"Authorization": f"Bearer {stu_tok}"})
    assert h1.status_code == 200
    assert len(h1.json()) == 1
    assert int(h1.headers["X-Total-Count"]) >= 2


def test_batch_answer_save_and_idempotent_finish_with_flush(client, db_session, admin_token, student_token):
    """Verify atomic batch answer upsert, flush-on-finish, and Idempotency-Key retries."""
    # Create a 3-question quiz
    qz = client.post(
        "/api/v1/quizzes",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"title": "Batch Quiz", "duration_minutes": 15, "passing_percentage": 50},
    ).json()
    quiz_id = qz["id"]

    q_ids = []
    for idx, corr in enumerate(["A", "B", "C"]):
        q = client.post(
            f"/api/v1/quizzes/{quiz_id}/questions",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "question_text": f"Batch Question {idx + 1}?",
                "option_a": "Opt A",
                "option_b": "Opt B",
                "option_c": "Opt C",
                "option_d": "Opt D",
                "correct_option": corr,
                "marks": 2,
            },
        ).json()
        q_ids.append(q["id"])

    client.post(f"/api/v1/quizzes/{quiz_id}/publish", headers={"Authorization": f"Bearer {admin_token}"})

    # Start attempt -> includes server_time
    start_res = client.post(
        f"/api/v1/attempts/quizzes/{quiz_id}/start",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert start_res.status_code == 201
    start_data = start_res.json()
    assert start_data["server_time"] is not None
    attempt_id = start_data["attempt_id"]

    # Batch save Q1 (initially wrong D, then updated to A in same batch) + Q2 (B)
    batch_res = client.post(
        f"/api/v1/attempts/{attempt_id}/answers/batch",
        headers={"Authorization": f"Bearer {student_token}"},
        json={
            "answers": [
                {"question_id": q_ids[0], "selected_option": "D"},
                {"question_id": q_ids[0], "selected_option": "A"},
                {"question_id": q_ids[1], "selected_option": "B"},
            ]
        },
    )
    assert batch_res.status_code == 200
    assert batch_res.json()["saved_count"] == 2

    # Finish attempt while flushing Q3 ("C") atomically with an Idempotency-Key
    idem_key = "retry-key-12345"
    fin1 = client.post(
        f"/api/v1/attempts/{attempt_id}/finish",
        headers={"Authorization": f"Bearer {student_token}", "Idempotency-Key": idem_key},
        json={"answers": [{"question_id": q_ids[2], "selected_option": "C"}]},
    )
    assert fin1.status_code == 200
    d1 = fin1.json()
    assert d1["score_obtained"] == 6.0
    assert d1["percentage"] == 100.0
    assert d1["correct_count"] == 3

    # Retry finish with SAME Idempotency-Key -> returns 200 with identical result!
    fin2 = client.post(
        f"/api/v1/attempts/{attempt_id}/finish",
        headers={"Authorization": f"Bearer {student_token}", "Idempotency-Key": idem_key},
    )
    assert fin2.status_code == 200
    assert fin2.json()["score_obtained"] == 6.0
    assert fin2.json()["attempt_id"] == attempt_id

    # Duplicate submit WITHOUT Idempotency-Key -> returns 400
    fin3 = client.post(
        f"/api/v1/attempts/{attempt_id}/finish",
        headers={"Authorization": f"Bearer {student_token}"},
    )
    assert fin3.status_code == 400


def test_concurrent_answer_saves_and_concurrent_finish_integrity(db_session):
    """
    True multi-threaded concurrency test:
    1. 10 concurrent threads UPSERT answers for the SAME (attempt_id, question_id) -> exactly 1 row!
    2. 8 concurrent threads race to call finish_quiz on the SAME attempt -> only 1 succeeds,
       score is 100% accurate, zero duplicate grading or corruption!
    """
    from tests.conftest import TestingSessionLocal

    org = User(email="conc_org@test.local", full_name="Conc Org", hashed_password="h", role="organizer", is_active=True)
    stu = User(email="conc_stu@test.local", full_name="Conc Stu", hashed_password="h", role="participant", is_active=True)
    db_session.add_all([org, stu])
    db_session.flush()

    qz = Quiz(
        title="Concurrency Exam",
        duration_minutes=15,
        passing_percentage=50,
        total_marks=5,
        is_published=True,
        is_public=True,
        max_attempts=1,
        created_by_id=org.id,
    )
    db_session.add(qz)
    db_session.flush()

    q = Question(
        quiz_id=qz.id,
        question_text="2 + 2 = ?",
        option_a="3",
        option_b="4",
        option_c="5",
        option_d="6",
        correct_option="B",
        marks=5,
        order_index=1,
    )
    db_session.add(q)
    db_session.commit()

    student_id = stu.id
    start_res = AttemptService.start_quiz(db_session, qz.id, student_id)
    attempt_id = start_res.attempt_id
    q_id = q.id

    from app.schemas.attempt import AnswerSubmitRequest

    def _save_worker(opt: str):
        s = TestingSessionLocal()
        try:
            AttemptService.save_answer(s, attempt_id, student_id, AnswerSubmitRequest(question_id=q_id, selected_option=opt))
            return True
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_save_worker, ["A", "B", "C", "B", "B", "A", "B", "B"]))
    assert all(results)

    # Verify ZERO duplicate rows in attempt_answers
    row_count = db_session.query(AttemptAnswer).filter(
        AttemptAnswer.attempt_id == attempt_id, AttemptAnswer.question_id == q_id
    ).count()
    assert row_count == 1

    # Set final option deterministically to B before concurrent finish race
    AttemptService.save_answer(db_session, attempt_id, student_id, AnswerSubmitRequest(question_id=q_id, selected_option="B"))

    def _finish_worker(_):
        s = TestingSessionLocal()
        try:
            res = AttemptService.finish_quiz(s, attempt_id, student_id, idempotent=False)
            return ("ok", res.score_obtained)
        except Exception as exc:
            return ("err", getattr(exc, "status_code", str(exc)))
        finally:
            s.close()

    with ThreadPoolExecutor(max_workers=6) as pool:
        outcomes = list(pool.map(_finish_worker, range(6)))

    successes = [o for o in outcomes if o[0] == "ok"]
    rejections = [o for o in outcomes if o[0] == "err" and o[1] == 400]
    assert len(successes) == 1, f"Expected exactly 1 winner in concurrent finish race, got: {outcomes}"
    assert successes[0][1] == 5.0
    assert len(rejections) == 5


def test_student_isolation_and_organizer_question_authorization(client, db_session):
    """Ensure student B cannot access student A's attempt, and organizer B cannot read organizer A's questions."""
    org1 = User(email="iso_o1@test.local", full_name="O1", hashed_password="h", role="organizer", is_active=True)
    org2 = User(email="iso_o2@test.local", full_name="O2", hashed_password="h", role="organizer", is_active=True)
    stu1 = User(email="iso_s1@test.local", full_name="S1", hashed_password="h", role="participant", is_active=True)
    stu2 = User(email="iso_s2@test.local", full_name="S2", hashed_password="h", role="participant", is_active=True)
    db_session.add_all([org1, org2, stu1, stu2])
    db_session.commit()

    tok_o1 = create_access_token(str(org1.id), org1.role, org1.email, org1.full_name)
    tok_o2 = create_access_token(str(org2.id), org2.role, org2.email, org2.full_name)
    tok_s1 = create_access_token(str(stu1.id), stu1.role, stu1.email, stu1.full_name)
    tok_s2 = create_access_token(str(stu2.id), stu2.role, stu2.email, stu2.full_name)

    qz = client.post(
        "/api/v1/quizzes",
        headers={"Authorization": f"Bearer {tok_o1}"},
        json={"title": "Private Org1 Quiz", "duration_minutes": 10, "passing_percentage": 50},
    ).json()
    q = client.post(
        f"/api/v1/quizzes/{qz['id']}/questions",
        headers={"Authorization": f"Bearer {tok_o1}"},
        json={
            "question_text": "Secret question?",
            "option_a": "1",
            "option_b": "2",
            "option_c": "3",
            "option_d": "4",
            "correct_option": "A",
        },
    ).json()
    client.post(f"/api/v1/quizzes/{qz['id']}/publish", headers={"Authorization": f"Bearer {tok_o1}"})

    # Organizer 2 must NOT read Organizer 1's questions (contains correct_option!)
    forbidden_q = client.get(
        f"/api/v1/quizzes/{qz['id']}/questions",
        headers={"Authorization": f"Bearer {tok_o2}"},
    )
    assert forbidden_q.status_code == 403

    # Student 1 starts attempt
    att = client.post(
        f"/api/v1/attempts/quizzes/{qz['id']}/start",
        headers={"Authorization": f"Bearer {tok_s1}"},
    ).json()

    # Student 2 must NOT save answer, finish, or read result of Student 1's attempt
    assert client.post(
        f"/api/v1/attempts/{att['attempt_id']}/answers",
        headers={"Authorization": f"Bearer {tok_s2}"},
        json={"question_id": q["id"], "selected_option": "A"},
    ).status_code == 403
    assert client.post(
        f"/api/v1/attempts/{att['attempt_id']}/finish",
        headers={"Authorization": f"Bearer {tok_s2}"},
    ).status_code == 403
    assert client.get(
        f"/api/v1/attempts/{att['attempt_id']}/result",
        headers={"Authorization": f"Bearer {tok_s2}"},
    ).status_code == 403


def test_bounded_cache_ttl_eviction_and_rate_limiter():
    """Verify bounded LRU cache eviction, TTL expiry, and rate limiting enforcement."""
    c = BoundedTTLCache(max_entries=16)
    for i in range(25):
        c.set(f"k{i}", f"v{i}", ttl=60)
    assert c.size() == 16
    assert c.get("k0") is None  # Evicted LRU entry
    assert c.get("k24") == "v24"

    c.set("short", "val", ttl=1)
    assert c.get("short") == "val"
    time.sleep(1.05)
    assert c.get("short") is None

    # Test rate limiter enforcement explicitly
    orig_enabled = settings.RATE_LIMIT_ENABLED
    try:
        settings.RATE_LIMIT_ENABLED = True
        rate_limiter.reset_local()
        for _ in range(3):
            allowed, _, _ = rate_limiter.check("test_bucket", "127.0.0.99", limit_per_minute=3)
            assert allowed is True
        allowed_4th, remaining, _ = rate_limiter.check("test_bucket", "127.0.0.99", limit_per_minute=3)
        assert allowed_4th is False
        assert remaining == 0
    finally:
        settings.RATE_LIMIT_ENABLED = orig_enabled
        rate_limiter.reset_local()


def test_readiness_metrics_and_background_csv_export(client, db_session, admin_token):
    """Verify /ready, /api/v1/admin/metrics, Prometheus metrics, and async background CSV export."""
    org0, stu0, quiz0 = _seed_medium_dataset(db_session)
    headers = {"Authorization": f"Bearer {admin_token}"}

    # Readiness probes
    assert client.get("/ready").status_code == 200
    assert client.get("/api/v1/ready").json()["ready"] is True

    # Metrics endpoints
    m_res = client.get("/api/v1/admin/metrics", headers=headers)
    assert m_res.status_code == 200
    m = m_res.json()
    assert "http" in m and "latency_ms" in m["http"]
    assert "database" in m and "cache" in m

    prom_res = client.get("/api/v1/admin/metrics/prometheus", headers=headers)
    assert prom_res.status_code == 200
    assert "quizflow_http_requests_total" in prom_res.text

    # Response diagnostic & security headers
    assert "X-DB-Query-Count" in m_res.headers
    assert "X-Process-Time-Ms" in m_res.headers
    assert m_res.headers["X-Content-Type-Options"] == "nosniff"

    # Async CSV background export task
    q_res = client.post(
        f"/api/v1/organizer/quizzes/{quiz0.id}/export-csv/async",
        headers=headers,
    )
    assert q_res.status_code == 202
    task_id = q_res.json()["task_id"]

    # Poll briefly until background worker finishes
    final_state = None
    for _ in range(30):
        st = client.get(f"/api/v1/organizer/tasks/{task_id}", headers=headers)
        assert st.status_code == 200
        final_state = st.json()
        if final_state["status"] in ("completed", "failed"):
            break
        time.sleep(0.05)

    assert final_state is not None
    assert final_state["status"] == "completed"
    assert "Attempt ID,Student Name" in final_state["content"]


def test_safe_migration_deduplication_preserves_data(db_session):
    """Verify apply_safe_migrations safely deduplicates legacy duplicate attempt_answers and creates indexes."""
    # Recreate attempt_answers WITHOUT unique constraint to simulate legacy pre-migration database
    db_session.execute(text("DROP TABLE attempt_answers"))
    db_session.execute(
        text(
            "CREATE TABLE attempt_answers ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "attempt_id INTEGER NOT NULL, "
            "question_id INTEGER NOT NULL, "
            "selected_option VARCHAR(1), "
            "is_correct BOOLEAN NOT NULL DEFAULT 0, "
            "marks_awarded FLOAT NOT NULL DEFAULT 0.0, "
            "answered_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
    )
    db_session.commit()

    org = User(email="mig_o@test.local", full_name="Mig O", hashed_password="h", role="organizer", is_active=True)
    stu = User(email="mig_s@test.local", full_name="Mig S", hashed_password="h", role="student", is_active=True)
    db_session.add_all([org, stu])
    db_session.flush()

    qz = Quiz(
        title="Mig Quiz", duration_minutes=10, passing_percentage=50, total_marks=1,
        is_published=True, is_public=True, created_by_id=org.id
    )
    db_session.add(qz)
    db_session.flush()
    q = Question(
        quiz_id=qz.id, question_text="Mig Q?", option_a="1", option_b="2", option_c="3", option_d="4",
        correct_option="A", marks=1, order_index=1
    )
    db_session.add(q)
    db_session.flush()
    att = QuizAttempt(
        student_id=stu.id, quiz_id=qz.id,
        deadline_time=datetime.now(timezone.utc) + timedelta(minutes=10),
        status=AttemptStatus.IN_PROGRESS.value,
    )
    db_session.add(att)
    db_session.flush()

    # Insert duplicate answers for same (attempt_id, question_id) in legacy un-indexed table
    db_session.execute(
        text("INSERT INTO attempt_answers (attempt_id, question_id, selected_option) VALUES (:a, :q, 'B')"),
        {"a": att.id, "q": q.id},
    )
    db_session.execute(
        text("INSERT INTO attempt_answers (attempt_id, question_id, selected_option) VALUES (:a, :q, 'A')"),
        {"a": att.id, "q": q.id},
    )
    db_session.commit()

    # Run safe migration twice (must be idempotent!)
    apply_safe_migrations(db_session)
    apply_safe_migrations(db_session)

    # Latest answer ('A') preserved, duplicates collapsed to 1 row, student role normalized
    remaining = db_session.query(AttemptAnswer).filter(AttemptAnswer.attempt_id == att.id).all()
    assert len(remaining) == 1
    assert remaining[0].selected_option == "A"
    db_session.refresh(stu)
    assert stu.role == "participant"
