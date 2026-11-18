"""
Performance Monitoring, Request Metrics, Query Tracking, and Structured Logging for QuizFlow.
Tracks p50/p95/p99 latency, RPS, error rate, DB query counts, slow queries, pool stats, CPU/RAM, and cache ratio.
Uses bounded memory structures (ring buffers) to guarantee zero memory leaks under sustained load.
"""
import contextvars
import json
import logging
import os
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, Optional

from app.core.config import settings

try:
    import psutil
except ImportError:
    psutil = None

# Per-request context variables for accurate query counting & timing across AnyIO threadpool workers.
# Using a mutable dict inside the ContextVar ensures increments made inside sync route worker threads
# propagate cleanly back to the outer async HTTP middleware!
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")
_request_stats_var: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "request_stats", default=None
)


def reset_request_query_stats(req_id: str = "") -> Dict[str, Any]:
    request_id_var.set(req_id)
    stats = {"queries": 0, "query_ms": 0.0}
    _request_stats_var.set(stats)
    return stats


def get_request_query_stats() -> tuple[int, float]:
    stats = _request_stats_var.get()
    if not stats:
        return 0, 0.0
    return int(stats["queries"]), float(stats["query_ms"])


def record_db_query(duration_ms: float, sql_summary: str) -> None:
    stats = _request_stats_var.get()
    if stats is not None:
        stats["queries"] += 1
        stats["query_ms"] += duration_ms
    metrics_collector.record_query(duration_ms, sql_summary)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 2)
    idx = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac, 2)


class MetricsCollector:
    """Thread-safe, bounded-memory performance metrics aggregator."""

    def __init__(self, window_size: int = 2000):
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._window_size = window_size

        self._total_requests = 0
        self._error_requests = 0
        self._latencies_ms: Deque[float] = deque(maxlen=window_size)
        self._request_timestamps: Deque[float] = deque(maxlen=window_size)
        self._route_latencies: Dict[str, Deque[float]] = defaultdict(lambda: deque(maxlen=300))
        self._route_counts: Dict[str, int] = defaultdict(int)
        self._route_errors: Dict[str, int] = defaultdict(int)
        self._route_queries: Dict[str, Deque[int]] = defaultdict(lambda: deque(maxlen=300))

        # DB Metrics
        self._total_queries = 0
        self._slow_queries = 0
        self._recent_slow_queries: Deque[Dict[str, Any]] = deque(maxlen=50)
        self._req_query_counts: Deque[int] = deque(maxlen=window_size)

        # Background Task Metrics
        self._bg_tasks_submitted = 0
        self._bg_tasks_succeeded = 0
        self._bg_tasks_failed = 0

        self._process = psutil.Process(os.getpid()) if psutil else None

    def record_request(
        self,
        route: str,
        status_code: int,
        duration_ms: float,
        query_count: int,
    ) -> None:
        now = time.monotonic()
        is_error = status_code >= 500
        with self._lock:
            self._total_requests += 1
            if is_error:
                self._error_requests += 1
            self._latencies_ms.append(duration_ms)
            self._request_timestamps.append(now)
            self._req_query_counts.append(query_count)

            self._route_counts[route] += 1
            if is_error:
                self._route_errors[route] += 1
            self._route_latencies[route].append(duration_ms)
            self._route_queries[route].append(query_count)

    def record_query(self, duration_ms: float, sql_summary: str) -> None:
        with self._lock:
            self._total_queries += 1
            if duration_ms >= settings.SLOW_QUERY_THRESHOLD_MS:
                self._slow_queries += 1
                self._recent_slow_queries.append(
                    {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "duration_ms": round(duration_ms, 2),
                        "sql": sql_summary[:240],
                    }
                )

    def record_bg_task(self, event: str) -> None:
        with self._lock:
            if event == "submitted":
                self._bg_tasks_submitted += 1
            elif event == "succeeded":
                self._bg_tasks_succeeded += 1
            elif event == "failed":
                self._bg_tasks_failed += 1

    def snapshot(self, db_pool_stats: Optional[Dict[str, Any]] = None, cache_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            uptime = max(0.001, now - self._started_at)
            lats = sorted(self._latencies_ms)
            # Rolling 60-second RPS
            cutoff = now - 60.0
            recent_count = sum(1 for ts in self._request_timestamps if ts >= cutoff)
            window_span = min(60.0, uptime)
            rps = round(recent_count / max(0.1, window_span), 2)

            error_rate = (
                round((self._error_requests / self._total_requests) * 100.0, 2)
                if self._total_requests > 0
                else 0.0
            )
            avg_queries = (
                round(sum(self._req_query_counts) / len(self._req_query_counts), 2)
                if self._req_query_counts
                else 0.0
            )
            max_queries = max(self._req_query_counts) if self._req_query_counts else 0

            route_breakdown = {}
            for r, cnt in sorted(self._route_counts.items(), key=lambda kv: kv[1], reverse=True)[:30]:
                rl = sorted(self._route_latencies[r])
                rq = self._route_queries[r]
                route_breakdown[r] = {
                    "count": cnt,
                    "errors": self._route_errors[r],
                    "p50_ms": _percentile(rl, 50),
                    "p95_ms": _percentile(rl, 95),
                    "p99_ms": _percentile(rl, 99),
                    "avg_queries": round(sum(rq) / len(rq), 2) if rq else 0.0,
                }

            slow_list = list(self._recent_slow_queries)[-10:]

            bg_stats = {
                "submitted": self._bg_tasks_submitted,
                "succeeded": self._bg_tasks_succeeded,
                "failed": self._bg_tasks_failed,
            }

            result = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "uptime_seconds": round(uptime, 1),
                "http": {
                    "total_requests": self._total_requests,
                    "server_errors_5xx": self._error_requests,
                    "error_rate_percent": error_rate,
                    "requests_per_second_60s": rps,
                    "latency_ms": {
                        "p50": _percentile(lats, 50),
                        "p95": _percentile(lats, 95),
                        "p99": _percentile(lats, 99),
                    },
                },
                "database": {
                    "total_queries": self._total_queries,
                    "avg_queries_per_request": avg_queries,
                    "max_queries_per_request": max_queries,
                    "slow_query_threshold_ms": settings.SLOW_QUERY_THRESHOLD_MS,
                    "slow_queries_count": self._slow_queries,
                    "recent_slow_queries": slow_list,
                    "pool": db_pool_stats or {},
                },
                "cache": cache_stats or {},
                "background_tasks": bg_stats,
                "system": self._system_stats(),
                "routes": route_breakdown,
            }
            return result

    def _system_stats(self) -> Dict[str, Any]:
        if not self._process:
            return {"pid": os.getpid(), "cpu_count": os.cpu_count()}
        try:
            mem = self._process.memory_info()
            return {
                "pid": os.getpid(),
                "cpu_count": os.cpu_count(),
                "process_cpu_percent": round(self._process.cpu_percent(interval=None), 2),
                "process_rss_mb": round(mem.rss / (1024 * 1024), 2),
                "system_memory_percent": round(psutil.virtual_memory().percent, 2),
            }
        except Exception:
            return {"pid": os.getpid(), "cpu_count": os.cpu_count()}

    def prometheus_text(self, db_pool_stats: Optional[Dict[str, Any]] = None, cache_stats: Optional[Dict[str, Any]] = None) -> str:
        snap = self.snapshot(db_pool_stats=db_pool_stats, cache_stats=cache_stats)
        lines = [
            "# HELP quizflow_http_requests_total Total HTTP requests processed",
            "# TYPE quizflow_http_requests_total counter",
            f"quizflow_http_requests_total {snap['http']['total_requests']}",
            "# HELP quizflow_http_errors_5xx_total Total HTTP 5xx errors",
            "# TYPE quizflow_http_errors_5xx_total counter",
            f"quizflow_http_errors_5xx_total {snap['http']['server_errors_5xx']}",
            "# HELP quizflow_http_latency_ms HTTP request latency percentiles in milliseconds",
            "# TYPE quizflow_http_latency_ms gauge",
            f'quizflow_http_latency_ms{{quantile="0.50"}} {snap["http"]["latency_ms"]["p50"]}',
            f'quizflow_http_latency_ms{{quantile="0.95"}} {snap["http"]["latency_ms"]["p95"]}',
            f'quizflow_http_latency_ms{{quantile="0.99"}} {snap["http"]["latency_ms"]["p99"]}',
            "# HELP quizflow_db_queries_total Total SQL queries executed",
            "# TYPE quizflow_db_queries_total counter",
            f"quizflow_db_queries_total {snap['database']['total_queries']}",
            "# HELP quizflow_db_slow_queries_total Total SQL queries exceeding slow threshold",
            "# TYPE quizflow_db_slow_queries_total counter",
            f"quizflow_db_slow_queries_total {snap['database']['slow_queries_count']}",
            "# HELP quizflow_cache_hit_ratio_percent Cache hit ratio percentage",
            "# TYPE quizflow_cache_hit_ratio_percent gauge",
            f"quizflow_cache_hit_ratio_percent {snap['cache'].get('hit_ratio_percent', 0.0)}",
            "# HELP quizflow_bg_tasks_failed_total Total failed background tasks",
            "# TYPE quizflow_bg_tasks_failed_total counter",
            f"quizflow_bg_tasks_failed_total {snap['background_tasks']['failed']}",
        ]
        if "process_rss_mb" in snap["system"]:
            lines.extend([
                "# HELP quizflow_process_rss_mb Process resident set memory in MB",
                "# TYPE quizflow_process_rss_mb gauge",
                f"quizflow_process_rss_mb {snap['system']['process_rss_mb']}",
            ])
        return "\n".join(lines) + "\n"


class JSONFormatter(logging.Formatter):
    """Structured JSON log formatter suitable for centralized logging (ELK/Datadog/CloudWatch)."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "pid": os.getpid(),
        }
        req_id = request_id_var.get()
        if req_id:
            payload["request_id"] = req_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    level = getattr(logging, settings.LOG_LEVEL, logging.INFO)
    root.setLevel(level)

    handler = logging.StreamHandler()
    if settings.LOG_FORMAT == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root.handlers = [handler]


metrics_collector = MetricsCollector()
