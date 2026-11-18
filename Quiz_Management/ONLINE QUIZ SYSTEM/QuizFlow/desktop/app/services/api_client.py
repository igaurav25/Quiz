"""
Centralized, Thread-Safe, Connection-Pooled API Client for QuizFlow Desktop Application.
Features:
- Persistent HTTP keep-alive connection pooling (HTTPAdapter) with automatic transient retries
- Non-blocking background execution helper (run_in_background) that safely marshals UI updates
  back to the Tkinter main thread via widget.after(0, ...)
- Support for pagination, batch answer autosave, and idempotent quiz finalization
"""
from concurrent.futures import ThreadPoolExecutor
import os
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_BASE_URL = os.getenv("QUIZFLOW_API_URL", "http://127.0.0.1:8000/api/v1")


class ApiClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.token: Optional[str] = None
        self.user_data: Optional[Dict[str, Any]] = None
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="qf-desktop-io-")

        self.session = requests.Session()
        retry_strategy = Retry(
            total=2,
            backoff_factor=0.25,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=16, pool_maxsize=32, max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def run_in_background(
        self,
        widget: Any,
        task_fn: Callable[[], Any],
        on_done: Callable[[Any], None],
    ) -> None:
        """
        Execute blocking network I/O on a worker thread and invoke on_done(result)
        strictly on the Tkinter UI thread if the target widget still exists.
        """
        def _worker():
            try:
                result = task_fn()
            except Exception as exc:
                result = (False, f"Unexpected error: {exc}")

            def _deliver():
                try:
                    if widget is not None and hasattr(widget, "winfo_exists") and not widget.winfo_exists():
                        return
                    on_done(result)
                except Exception:
                    pass

            try:
                if widget is not None and hasattr(widget, "after"):
                    widget.after(0, _deliver)
                else:
                    on_done(result)
            except Exception:
                pass

        self._executor.submit(_worker)

    def set_base_url(self, url: str) -> None:
        with self._lock:
            self.base_url = url.rstrip("/")

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept-Encoding": "gzip"}
        with self._lock:
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
        if extra:
            headers.update(extra)
        return headers

    def _request(
        self,
        method: str,
        path: str,
        json_data: Any = None,
        params: Optional[Dict[str, Any]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
        timeout: int = 12,
    ) -> Tuple[bool, Any]:
        url = f"{self.base_url}{path}"
        try:
            res = self.session.request(
                method=method,
                url=url,
                headers=self._headers(extra_headers),
                json=json_data,
                params=params,
                timeout=timeout,
            )
            if res.status_code in (200, 201, 202):
                return True, res.json()
            try:
                err_data = res.json()
                msg = err_data.get("detail", res.text)
                return False, msg
            except Exception:
                return False, f"Server Error ({res.status_code}): {res.text}"
        except requests.exceptions.ConnectionError:
            return False, "Cannot connect to QuizFlow server. Ensure backend is running at " + self.base_url
        except requests.exceptions.Timeout:
            return False, "Network request timed out. Please check your connection."
        except Exception as e:
            return False, f"Unexpected network error: {str(e)}"

    # System Health
    def check_health(self) -> Tuple[bool, Any]:
        return self._request("GET", "/health", timeout=5)

    # Authentication
    def login(self, email: str, password: str) -> Tuple[bool, Any]:
        ok, data = self._request("POST", "/auth/login", {"email": email, "password": password})
        if ok and "access_token" in data:
            with self._lock:
                self.token = data["access_token"]
                self.user_data = data.get("user")
        return ok, data

    def register(
        self,
        email: str,
        full_name: str,
        password: str,
        confirm_password: str,
        role: str = "participant",
    ) -> Tuple[bool, Any]:
        return self._request(
            "POST",
            "/auth/register",
            {
                "email": email,
                "full_name": full_name,
                "password": password,
                "confirm_password": confirm_password,
                "role": role,
            },
        )

    def logout(self) -> None:
        with self._lock:
            self.token = None
            self.user_data = None

    def get_me(self) -> Tuple[bool, Any]:
        return self._request("GET", "/auth/me")

    # Admin Operations
    def get_dashboard_stats(self) -> Tuple[bool, Any]:
        return self._request("GET", "/admin/dashboard-stats")

    def get_admin_quizzes(self, limit: int = 100, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", "/quizzes/admin/all", params={"limit": limit, "offset": offset})

    def create_quiz(self, quiz_data: Dict[str, Any]) -> Tuple[bool, Any]:
        return self._request("POST", "/quizzes", quiz_data)

    def update_quiz(self, quiz_id: int, quiz_data: Dict[str, Any]) -> Tuple[bool, Any]:
        return self._request("PUT", f"/quizzes/{quiz_id}", quiz_data)

    def delete_quiz(self, quiz_id: int) -> Tuple[bool, Any]:
        return self._request("DELETE", f"/quizzes/{quiz_id}")

    def publish_quiz(self, quiz_id: int) -> Tuple[bool, Any]:
        return self._request("POST", f"/quizzes/{quiz_id}/publish")

    def unpublish_quiz(self, quiz_id: int) -> Tuple[bool, Any]:
        return self._request("POST", f"/quizzes/{quiz_id}/unpublish")

    def get_quiz_questions(self, quiz_id: int, limit: int = 200, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", f"/quizzes/{quiz_id}/questions", params={"limit": limit, "offset": offset})

    def add_question(self, quiz_id: int, q_data: Dict[str, Any]) -> Tuple[bool, Any]:
        return self._request("POST", f"/quizzes/{quiz_id}/questions", q_data)

    def update_question(self, question_id: int, q_data: Dict[str, Any]) -> Tuple[bool, Any]:
        return self._request("PUT", f"/questions/{question_id}", q_data)

    def delete_question(self, question_id: int) -> Tuple[bool, Any]:
        return self._request("DELETE", f"/questions/{question_id}")

    def get_students(self, limit: int = 100, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", "/admin/students", params={"limit": limit, "offset": offset})

    def get_admin_users(
        self,
        role: Optional[str] = None,
        is_active: Optional[bool] = None,
        search: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Tuple[bool, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if role:
            params["role"] = role
        if is_active is not None:
            params["is_active"] = "true" if is_active else "false"
        if search:
            params["search"] = search
        return self._request("GET", "/admin/users", params=params)

    def update_user_status(self, user_id: int, is_active: bool, reason: str = "") -> Tuple[bool, Any]:
        return self._request("POST", f"/admin/users/{user_id}/status", {"is_active": is_active, "reason": reason})

    def moderate_quiz(self, quiz_id: int, is_disabled: bool, reason: str = "") -> Tuple[bool, Any]:
        return self._request("POST", f"/admin/quizzes/{quiz_id}/moderate", {"is_disabled": is_disabled, "reason": reason})

    def get_audit_logs(
        self, action: Optional[str] = None, user_id: Optional[int] = None, limit: int = 50, offset: int = 0
    ) -> Tuple[bool, Any]:
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        if action:
            params["action"] = action
        if user_id:
            params["user_id"] = user_id
        return self._request("GET", "/admin/audit-logs", params=params)

    def get_platform_reports(self) -> Tuple[bool, Any]:
        return self._request("GET", "/admin/platform-reports")

    def get_all_results(self, limit: int = 100, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", "/admin/results", params={"limit": limit, "offset": offset})

    def get_performance_metrics(self) -> Tuple[bool, Any]:
        return self._request("GET", "/admin/metrics")

    # Organizer Operations
    def get_organizer_stats(self) -> Tuple[bool, Any]:
        return self._request("GET", "/organizer/dashboard-stats")

    def get_organizer_quizzes(self, limit: int = 100, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", "/organizer/quizzes", params={"limit": limit, "offset": offset})

    def get_organizer_results(self, limit: int = 100, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", "/organizer/results", params={"limit": limit, "offset": offset})

    def get_quiz_report(self, quiz_id: int, attempts_limit: int = 100, attempts_offset: int = 0) -> Tuple[bool, Any]:
        return self._request(
            "GET",
            f"/organizer/quizzes/{quiz_id}/report",
            params={"attempts_limit": attempts_limit, "attempts_offset": attempts_offset},
        )

    def export_quiz_csv(self, quiz_id: int) -> Tuple[bool, Any]:
        url = f"{self.base_url}/organizer/quizzes/{quiz_id}/export-csv"
        try:
            res = self.session.get(url, headers=self._headers(), timeout=20)
            if res.status_code == 200:
                return True, res.text
            return False, res.text
        except Exception as e:
            return False, f"Failed to download CSV: {str(e)}"

    def release_results(self, quiz_id: int) -> Tuple[bool, Any]:
        return self._request("POST", f"/organizer/quizzes/{quiz_id}/release-results")

    # AI & File Generation Operations
    def generate_quiz_ai(
        self,
        prompt: str,
        num_questions: int = 5,
        difficulty: str = "medium",
        topic: Optional[str] = None,
        save_directly: bool = False,
    ) -> Tuple[bool, Any]:
        payload = {
            "prompt": prompt,
            "num_questions": num_questions,
            "difficulty": difficulty,
            "topic": topic,
            "save_directly": save_directly,
        }
        return self._request("POST", "/organizer/ai/generate-quiz", payload, timeout=25)

    def import_quiz_file(
        self,
        file_content: str,
        filename: str,
        num_questions: int = 5,
        difficulty: str = "medium",
        save_directly: bool = False,
    ) -> Tuple[bool, Any]:
        payload = {
            "file_content": file_content,
            "filename": filename,
            "num_questions": num_questions,
            "difficulty": difficulty,
            "save_directly": save_directly,
        }
        return self._request("POST", "/organizer/ai/import-file-quiz", payload, timeout=20)

    # Participant / Student Operations
    def get_published_quizzes(self, limit: int = 100, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", "/quizzes", params={"limit": limit, "offset": offset})

    def get_quiz_by_code(self, join_code: str) -> Tuple[bool, Any]:
        return self._request("GET", f"/quizzes/by-code/{join_code.strip().upper()}")

    def join_quiz_by_code(self, join_code: str) -> Tuple[bool, Any]:
        return self._request("POST", "/quizzes/join-by-code", {"join_code": join_code.strip().upper()})

    def get_quiz_details(self, quiz_id: int) -> Tuple[bool, Any]:
        return self._request("GET", f"/quizzes/{quiz_id}")

    def start_quiz(self, quiz_id: int) -> Tuple[bool, Any]:
        return self._request("POST", f"/attempts/quizzes/{quiz_id}/start")

    def save_answer(self, attempt_id: int, question_id: int, selected_option: Optional[str]) -> Tuple[bool, Any]:
        return self._request(
            "POST",
            f"/attempts/{attempt_id}/answers",
            {"question_id": question_id, "selected_option": selected_option},
        )

    def save_answers_batch(
        self, attempt_id: int, answers: List[Dict[str, Any]]
    ) -> Tuple[bool, Any]:
        return self._request(
            "POST",
            f"/attempts/{attempt_id}/answers/batch",
            {"answers": answers},
        )

    def finish_quiz(
        self,
        attempt_id: int,
        idempotency_key: Optional[str] = None,
        pending_answers: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[bool, Any]:
        extra = {"Idempotency-Key": idempotency_key or uuid.uuid4().hex}
        payload = {"answers": pending_answers} if pending_answers else None
        return self._request(
            "POST",
            f"/attempts/{attempt_id}/finish",
            json_data=payload,
            extra_headers=extra,
            timeout=20,
        )

    def get_result(self, attempt_id: int) -> Tuple[bool, Any]:
        return self._request("GET", f"/attempts/{attempt_id}/result")

    def get_my_history(self, limit: int = 100, offset: int = 0) -> Tuple[bool, Any]:
        return self._request("GET", "/attempts/my-history", params={"limit": limit, "offset": offset})

    def get_student_dashboard_stats(self) -> Tuple[bool, Any]:
        return self._request("GET", "/attempts/my-dashboard-stats")


# Global desktop API client singleton
api_client = ApiClient()
