"""
Production Multi-Worker Server Launcher for QuizFlow.
Computes CPU-aware worker count (clamped to avoid oversubscribing DB connections),
enables proxy headers for Nginx/Load Balancer deployments, and configures graceful timeouts.

Usage:
  python run_prod.py
  WORKERS=4 PORT=8000 python run_prod.py
"""
import multiprocessing
import os
import uvicorn


def recommended_workers() -> int:
    env_workers = os.getenv("WORKERS")
    if env_workers:
        try:
            return max(1, int(env_workers))
        except ValueError:
            pass
    cpus = multiprocessing.cpu_count()
    db_url = os.getenv("DATABASE_URL", "sqlite:///./quizflow.db")
    if db_url.startswith("sqlite"):
        # SQLite supports 1 concurrent writer; keep workers conservative to minimize lock contention
        return min(4, max(2, cpus // 4))
    # PostgreSQL: standard (2 * cores + 1) clamped to protect DB connection budget
    return min(8, max(2, cpus // 2))


if __name__ == "__main__":
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))
    workers = recommended_workers()
    backlog = int(os.getenv("UVICORN_BACKLOG", "2048"))
    timeout_keep_alive = int(os.getenv("KEEP_ALIVE_TIMEOUT", "15"))
    timeout_graceful_shutdown = int(os.getenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30"))

    print(
        f"Starting QuizFlow production server on {host}:{port} "
        f"(workers={workers}, backlog={backlog}, graceful_shutdown={timeout_graceful_shutdown}s)"
    )

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        workers=workers,
        backlog=backlog,
        timeout_keep_alive=timeout_keep_alive,
        timeout_graceful_shutdown=timeout_graceful_shutdown,
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
        access_log=False,  # Handled by structured middleware logger with latency & DB query counts
    )
