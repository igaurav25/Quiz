"""
Reproducible Incremental Load-Testing Runner for QuizFlow.
Runs Locust headless across incremental concurrency tiers:
  50 -> 100 -> 250 -> 500 -> 1,000 simultaneous virtual users
plus a synchronized Peak Simultaneous Submission Spike Test.
Captures real p50, p95, p99, RPS, error rate, CPU, RAM, and backend /admin/metrics telemetry.
"""
import asyncio
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psutil

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
LOAD_DIR = Path(__file__).resolve().parent
RESULTS_DIR = LOAD_DIR / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

PYTHON_EXE = sys.executable
PORT = int(os.getenv("LOADTEST_PORT", "8099"))
HOST = f"http://127.0.0.1:{PORT}"
WORKERS = int(os.getenv("LOADTEST_WORKERS", "4"))


def wait_for_server(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/ready", timeout=2.0)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Server at {url} did not become ready within {timeout}s")


def parse_locust_stats(csv_path: Path) -> dict:
    with csv_path.open("r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    agg = next((r for r in rows if r.get("Name") == "Aggregated"), None)
    if not agg:
        return {}
    req_cnt = int(agg.get("Request Count", 0) or 0)
    fail_cnt = int(agg.get("Failure Count", 0) or 0)
    return {
        "requests": req_cnt,
        "failures": fail_cnt,
        "error_rate_pct": round((fail_cnt / req_cnt) * 100.0, 2) if req_cnt else 0.0,
        "rps": round(float(agg.get("Requests/s", 0) or 0), 2),
        "p50_ms": float(agg.get("50%", 0) or 0),
        "p95_ms": float(agg.get("95%", 0) or 0),
        "p99_ms": float(agg.get("99%", 0) or 0),
        "avg_ms": round(float(agg.get("Average Response Time", 0) or 0), 2),
    }


def main():
    # 1. Seed fresh isolated load-test DB
    print("=== Step 1: Seeding isolated load-test database ===")
    subprocess.run([PYTHON_EXE, str(LOAD_DIR / "seed_load_test_db.py")], check=True)
    creds = json.loads((LOAD_DIR / "loadtest_credentials.json").read_text(encoding="utf-8"))
    admin_headers = {"Authorization": f"Bearer {creds['admin']['token']}"}

    # 2. Launch QuizFlow server with production settings against isolated loadtest DB
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{LOAD_DIR / 'loadtest_quizflow.db'}"
    env["ENVIRONMENT"] = "development"
    env["DEBUG"] = "False"
    env["LOG_LEVEL"] = "WARNING"
    env["RATE_LIMIT_ENABLED"] = "False"  # Measure true infrastructure capacity without artificial 429 caps
    env["CACHE_ENABLED"] = "True"
    env["SECRET_KEY"] = "quizflow_loadtest_secret_key_at_least_32_characters_2026"
    env["WORKERS"] = str(WORKERS)
    env["HOST"] = "127.0.0.1"
    env["PORT"] = str(PORT)

    print(f"=== Step 2: Launching QuizFlow API ({WORKERS} workers on {HOST}) ===")
    server_proc = subprocess.Popen(
        [PYTHON_EXE, str(BACKEND_DIR / "run_prod.py")],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    tiers = [
        (50, 25, "25s"),
        (100, 50, "25s"),
        (250, 100, "25s"),
        (500, 125, "25s"),
        (1000, 200, "25s"),
    ]
    report = {
        "hardware": {
            "cpu_logical": os.cpu_count(),
            "ram_total_gb": round(psutil.virtual_memory().total / (1024**3), 2),
            "workers": WORKERS,
            "database": "SQLite (WAL mode, busy_timeout=15s)",
            "cache": "Bounded local LRU (Redis fallback)",
        },
        "locust_tiers": [],
        "peak_submission_spikes": [],
    }

    try:
        wait_for_server(HOST, timeout=30.0)
        print("Server is READY!\n")

        for users, spawn_rate, duration in tiers:
            print(f"--- Running Locust tier: {users} simultaneous users (spawn {spawn_rate}/s, duration {duration}) ---")
            prefix = RESULTS_DIR / f"tier_{users}"
            cpu_before = psutil.cpu_percent(interval=None)
            cmd = [
                PYTHON_EXE,
                "-m",
                "locust",
                "-f",
                str(LOAD_DIR / "locustfile.py"),
                "--host",
                HOST,
                "--headless",
                "-u",
                str(users),
                "-r",
                str(spawn_rate),
                "-t",
                duration,
                "--csv",
                str(prefix),
                "--only-summary",
            ]
            subprocess.run(cmd, cwd=str(ROOT_DIR), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            cpu_after = psutil.cpu_percent(interval=0.2)
            mem_pct = psutil.virtual_memory().percent

            stats = parse_locust_stats(Path(f"{prefix}_stats.csv"))
            stats["users"] = users
            stats["host_cpu_pct"] = round(cpu_after, 1)
            stats["host_mem_pct"] = round(mem_pct, 1)

            # Query backend /admin/metrics snapshot
            try:
                m_res = httpx.get(f"{HOST}/api/v1/admin/metrics", headers=admin_headers, timeout=5.0)
                if m_res.status_code == 200:
                    m = m_res.json()
                    stats["avg_db_queries_per_req"] = m["database"]["avg_queries_per_request"]
                    stats["slow_queries_count"] = m["database"]["slow_queries_count"]
                    stats["cache_hit_ratio_pct"] = m["cache"].get("hit_ratio_percent", 0.0)
            except Exception:
                pass

            report["locust_tiers"].append(stats)
            print(
                f"  Users={users:4d} | Req={stats.get('requests',0):6d} | RPS={stats.get('rps',0):6.1f} | "
                f"p50={stats.get('p50_ms',0):6.1f}ms | p95={stats.get('p95_ms',0):7.1f}ms | "
                f"p99={stats.get('p99_ms',0):7.1f}ms | Err={stats.get('error_rate_pct',0):5.2f}% | "
                f"CPU={cpu_after:4.1f}% | AvgDBQ={stats.get('avg_db_queries_per_req','-')}"
            )

        # 3. Run Peak Simultaneous Submission Spikes (50, 150, 300 simultaneous submitters)
        print("\n=== Step 3: Peak Simultaneous Submission Spike Tests ===")
        sys.path.insert(0, str(LOAD_DIR))
        from peak_submission_test import run_peak_spike

        for idx, spike_n in enumerate([50, 150, 300]):
            spike_res = asyncio.run(run_peak_spike(HOST, concurrency=spike_n, offset_student=400 + idx * 250))
            report["peak_submission_spikes"].append(spike_res)
            print(
                f"  Spike={spike_n:3d} simultaneous submits | Succeeded={spike_res['succeeded']}/{spike_res['prepared_attempts']} | "
                f"Submits/s={spike_res['submissions_per_sec']:6.1f} | "
                f"p50={spike_res['p50_ms']:6.1f}ms | p95={spike_res['p95_ms']:7.1f}ms | "
                f"p99={spike_res['p99_ms']:7.1f}ms | Err={spike_res['error_rate_pct']}%"
            )

        out_json = RESULTS_DIR / "load_test_summary.json"
        out_json.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nFull load-test report saved to: {out_json}")

    finally:
        print("\nStopping QuizFlow load-test server...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()


if __name__ == "__main__":
    main()
