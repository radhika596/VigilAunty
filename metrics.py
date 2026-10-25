"""Metric computation: request counts, error rate, latency percentiles, availability."""
import math
import sqlite3
import time
from typing import Optional

METRIC_NAMES = (
    "error_rate",
    "p50_latency_ms",
    "p95_latency_ms",
    "p99_latency_ms",
    "availability",
)


def percentile(sorted_values: list[float], p: float) -> Optional[float]:
    """Nearest-rank percentile on an already-sorted list."""
    if not sorted_values:
        return None
    rank = max(0, math.ceil(p / 100 * len(sorted_values)) - 1)
    return sorted_values[rank]


def service_metrics(
    conn: sqlite3.Connection,
    service_id: int,
    window_minutes: int,
    now: Optional[float] = None,
) -> dict:
    now = now if now is not None else time.time()
    since = now - window_minutes * 60

    rows = conn.execute(
        "SELECT latency_ms, status_code FROM events "
        "WHERE service_id = ? AND ts >= ? ORDER BY latency_ms",
        (service_id, since),
    ).fetchall()
    latencies = [r["latency_ms"] for r in rows]
    total = len(rows)
    errors = sum(1 for r in rows if r["status_code"] >= 500)

    hb = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(up), 0) AS up FROM heartbeats "
        "WHERE service_id = ? AND ts >= ?",
        (service_id, since),
    ).fetchone()

    return {
        "window_minutes": window_minutes,
        "request_count": total,
        "error_count": errors,
        "error_rate": round(errors / total * 100, 3) if total else None,
        "p50_latency_ms": percentile(latencies, 50),
        "p95_latency_ms": percentile(latencies, 95),
        "p99_latency_ms": percentile(latencies, 99),
        "availability": round(hb["up"] / hb["n"] * 100, 3) if hb["n"] else None,
    }
