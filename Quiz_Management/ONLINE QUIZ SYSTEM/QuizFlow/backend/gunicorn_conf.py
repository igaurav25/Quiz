"""
Gunicorn + UvicornWorker production configuration for Linux / Docker deployments.
Run with:
  gunicorn app.main:app -c gunicorn_conf.py
"""
import multiprocessing
import os

bind = f"{os.getenv('HOST', '0.0.0.0')}:{os.getenv('PORT', '8000')}"
worker_class = "uvicorn.workers.UvicornWorker"
workers = int(os.getenv("WORKERS", str(min(8, max(2, multiprocessing.cpu_count() // 2)))))
backlog = int(os.getenv("GUNICORN_BACKLOG", "2048"))
timeout = int(os.getenv("REQUEST_TIMEOUT", "60"))
graceful_timeout = int(os.getenv("GRACEFUL_SHUTDOWN_TIMEOUT", "30"))
keepalive = int(os.getenv("KEEP_ALIVE_TIMEOUT", "15"))
max_requests = int(os.getenv("MAX_REQUESTS_PER_WORKER", "10000"))
max_requests_jitter = int(os.getenv("MAX_REQUESTS_JITTER", "1000"))
forwarded_allow_ips = os.getenv("FORWARDED_ALLOW_IPS", "*")
accesslog = None  # Structured logging handled in FastAPI middleware
errorlog = "-"
loglevel = os.getenv("LOG_LEVEL", "info").lower()
