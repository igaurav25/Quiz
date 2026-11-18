"""
QuizFlow Realistic Multi-Persona Locust Load Test Suite.

Personas:
  1. QuizStudentUser (85% weight):
     Browse quizzes -> View quiz detail -> Start quiz -> Save batch answers -> Submit (idempotent) -> View result & history
  2. AdminDashboardUser (10% weight):
     Dashboard stats -> Paginated users -> Paginated results -> Platform reports -> Metrics
  3. OrganizerUser (5% weight):
     Organizer dashboard stats -> My quizzes -> Paginated quiz report

Run interactively:
  locust -f load_tests/locustfile.py --host http://127.0.0.1:8000

Run headless (example: 250 users for 45s):
  locust -f load_tests/locustfile.py --host http://127.0.0.1:8000 --headless -u 250 -r 50 -t 45s
"""
import itertools
import json
import os
import random
import uuid
from pathlib import Path

from locust import HttpUser, between, task

CREDS_PATH = Path(__file__).resolve().parent / "loadtest_credentials.json"
if not CREDS_PATH.exists():
    raise RuntimeError(
        f"Credentials file missing at {CREDS_PATH}. Run `python load_tests/seed_load_test_db.py` first."
    )

_CREDS = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
_STUDENT_CYCLE = itertools.cycle(_CREDS["students"])
_ORG_CYCLE = itertools.cycle(_CREDS["organizers"])
_QUIZ_IDS = _CREDS["quiz_ids"]
_ADMIN = _CREDS["admin"]
# Set LOADTEST_REAL_LOGIN=1 to include Argon2 login call on every virtual user startup
_USE_REAL_LOGIN = os.getenv("LOADTEST_REAL_LOGIN", "0") == "1"


class QuizStudentUser(HttpUser):
    weight = 85
    wait_time = between(0.8, 2.5)

    def on_start(self):
        self.creds = next(_STUDENT_CYCLE)
        self.token = self.creds["token"]
        if _USE_REAL_LOGIN:
            with self.client.post(
                "/api/v1/auth/login",
                json={"email": self.creds["email"], "password": self.creds["password"]},
                name="POST /api/v1/auth/login",
                catch_response=True,
            ) as res:
                if res.status_code == 200:
                    self.token = res.json()["access_token"]
                else:
                    res.failure(f"Login failed: {res.status_code}")

        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.active_attempt = None

    @task(3)
    def browse_quizzes_and_history(self):
        self.client.get(
            "/api/v1/quizzes?limit=20&offset=0",
            headers=self.headers,
            name="GET /api/v1/quizzes (paginated)",
        )
        self.client.get(
            "/api/v1/attempts/my-history?limit=20&offset=0",
            headers=self.headers,
            name="GET /api/v1/attempts/my-history",
        )

    @task(5)
    def complete_quiz_lifecycle(self):
        quiz_id = random.choice(_QUIZ_IDS)

        # 1. Inspect quiz details
        self.client.get(
            f"/api/v1/quizzes/{quiz_id}",
            headers=self.headers,
            name="GET /api/v1/quizzes/{id}",
        )

        # 2. Start attempt
        with self.client.post(
            f"/api/v1/attempts/quizzes/{quiz_id}/start",
            headers=self.headers,
            name="POST /api/v1/attempts/quizzes/{id}/start",
            catch_response=True,
        ) as start_res:
            if start_res.status_code == 400 and "maximum allowed attempts" in start_res.text.lower():
                start_res.success()
                return
            if start_res.status_code not in (200, 201):
                start_res.failure(f"Start failed: {start_res.status_code} {start_res.text[:100]}")
                return
            attempt_data = start_res.json()

        attempt_id = attempt_data["attempt_id"]
        questions = attempt_data.get("questions", [])
        if not questions:
            return

        # 3. Autosave first half in a debounced batch
        half = max(1, len(questions) // 2)
        batch_1 = [
            {"question_id": q["id"], "selected_option": random.choice(["A", "B", "C", "D"])}
            for q in questions[:half]
        ]
        self.client.post(
            f"/api/v1/attempts/{attempt_id}/answers/batch",
            json={"answers": batch_1},
            headers=self.headers,
            name="POST /api/v1/attempts/{id}/answers/batch",
        )

        # 4. Submit attempt atomically flushing remaining answers with Idempotency-Key
        batch_2 = [
            {"question_id": q["id"], "selected_option": random.choice(["A", "B", "C", "D"])}
            for q in questions[half:]
        ]
        fin_headers = {**self.headers, "Idempotency-Key": uuid.uuid4().hex}
        with self.client.post(
            f"/api/v1/attempts/{attempt_id}/finish",
            json={"answers": batch_2},
            headers=fin_headers,
            name="POST /api/v1/attempts/{id}/finish",
            catch_response=True,
        ) as fin_res:
            if fin_res.status_code != 200:
                fin_res.failure(f"Finish failed: {fin_res.status_code} {fin_res.text[:100]}")
                return

        # 5. View detailed result
        self.client.get(
            f"/api/v1/attempts/{attempt_id}/result",
            headers=self.headers,
            name="GET /api/v1/attempts/{id}/result",
        )


class AdminDashboardUser(HttpUser):
    weight = 10
    wait_time = between(1.5, 3.5)

    def on_start(self):
        self.headers = {"Authorization": f"Bearer {_ADMIN['token']}"}

    @task(3)
    def view_dashboard_and_reports(self):
        self.client.get(
            "/api/v1/admin/dashboard-stats",
            headers=self.headers,
            name="GET /api/v1/admin/dashboard-stats",
        )
        self.client.get(
            "/api/v1/admin/platform-reports",
            headers=self.headers,
            name="GET /api/v1/admin/platform-reports",
        )

    @task(2)
    def browse_paginated_admin_tables(self):
        self.client.get(
            "/api/v1/admin/users?limit=50&offset=0",
            headers=self.headers,
            name="GET /api/v1/admin/users (paginated)",
        )
        self.client.get(
            "/api/v1/admin/results?limit=50&offset=0",
            headers=self.headers,
            name="GET /api/v1/admin/results (paginated)",
        )


class OrganizerUser(HttpUser):
    weight = 5
    wait_time = between(1.5, 3.5)

    def on_start(self):
        self.creds = next(_ORG_CYCLE)
        self.headers = {"Authorization": f"Bearer {self.creds['token']}"}

    @task
    def view_organizer_workspace(self):
        self.client.get(
            "/api/v1/organizer/dashboard-stats",
            headers=self.headers,
            name="GET /api/v1/organizer/dashboard-stats",
        )
        res = self.client.get(
            "/api/v1/organizer/quizzes?limit=20&offset=0",
            headers=self.headers,
            name="GET /api/v1/organizer/quizzes",
        )
        if res.status_code == 200:
            quizzes = res.json()
            if quizzes:
                qid = quizzes[0]["id"]
                self.client.get(
                    f"/api/v1/organizer/quizzes/{qid}/report?attempts_limit=50&attempts_offset=0",
                    headers=self.headers,
                    name="GET /api/v1/organizer/quizzes/{id}/report",
                )
