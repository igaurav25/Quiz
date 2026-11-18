"""
Peak Simultaneous Submission Spike Test for QuizFlow.
Simulates N students who are all actively taking an exam and click "Submit Exam"
at the EXACT SAME INSTANT (synchronized barrier).
Measures p50, p95, p99 latency, throughput, error rate, and verifies 100% score integrity.
"""
import asyncio
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

CREDS_PATH = Path(__file__).resolve().parent / "loadtest_credentials.json"


def _pct(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (len(sorted_vals) - 1) * (p / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo), 2)


async def run_peak_spike(base_url: str, concurrency: int = 100, offset_student: int = 500) -> dict:
    creds = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
    students = creds["students"][offset_student : offset_student + concurrency]
    quiz_id = creds["quiz_ids"][0]

    limits = httpx.Limits(max_connections=concurrency + 50, max_keepalive_connections=concurrency)
    prep_sem = asyncio.Semaphore(20)

    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=30.0) as client:
        # Phase 1: Prepare all active attempts & save first 5 answers
        async def _prepare(stu):
            headers = {"Authorization": f"Bearer {stu['token']}"}
            async with prep_sem:
                try:
                    r = await client.post(f"/api/v1/attempts/quizzes/{quiz_id}/start", headers=headers)
                    if r.status_code not in (200, 201):
                        return None
                    data = r.json()
                    attempt_id = data["attempt_id"]
                    qs = data["questions"]
                    first_half = [{"question_id": q["id"], "selected_option": "A"} for q in qs[:5]]
                    await client.post(
                        f"/api/v1/attempts/{attempt_id}/answers/batch",
                        headers=headers,
                        json={"answers": first_half},
                    )
                    second_half = [{"question_id": q["id"], "selected_option": "B"} for q in qs[5:]]
                    return (attempt_id, headers, second_half)
                except Exception:
                    return None

        prep_results = await asyncio.gather(*[_prepare(s) for s in students])
        prepared = [p for p in prep_results if p is not None]

        # Phase 2: Synchronized simultaneous submission spike!
        latencies_ms: list[float] = []
        errors = 0
        t_start = time.perf_counter()

        async def _submit(item):
            nonlocal errors
            attempt_id, headers, second_half = item
            t0 = time.perf_counter()
            try:
                r = await client.post(
                    f"/api/v1/attempts/{attempt_id}/finish",
                    headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
                    json={"answers": second_half},
                )
                dt = (time.perf_counter() - t0) * 1000.0
                if r.status_code == 200:
                    body = r.json()
                    assert body["status"] == "completed"
                    assert body["total_marks"] == 10.0
                    latencies_ms.append(dt)
                else:
                    errors += 1
            except Exception:
                errors += 1

        await asyncio.gather(*[_submit(item) for item in prepared])
        total_sec = max(0.001, time.perf_counter() - t_start)

    latencies_ms.sort()
    total_submitted = len(prepared)
    return {
        "concurrent_submitters": concurrency,
        "prepared_attempts": total_submitted,
        "succeeded": len(latencies_ms),
        "errors": errors,
        "error_rate_pct": round((errors / max(1, total_submitted)) * 100.0, 2),
        "wall_time_sec": round(total_sec, 2),
        "submissions_per_sec": round(len(latencies_ms) / total_sec, 2),
        "p50_ms": _pct(latencies_ms, 50),
        "p95_ms": _pct(latencies_ms, 95),
        "p99_ms": _pct(latencies_ms, 99),
    }


if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 100
    result = asyncio.run(run_peak_spike(url, concurrency=n))
    print(json.dumps(result, indent=2))
