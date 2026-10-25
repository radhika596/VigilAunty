"""Simulate traffic from several services and report ingest throughput.
Usage: python scripts/load_test.py [base_url] [total_events]"""
import random
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TOTAL = int(sys.argv[2]) if len(sys.argv) > 2 else 50_000
BATCH = 500
SERVICES = ["checkout", "auth", "search", "payments", "inventory"]
ENDPOINTS = ["/v1/items", "/v1/login", "/v1/query", "/v1/pay"]


def make_event():
    svc = random.choice(SERVICES)
    bad = svc == "payments" and random.random() < 0.15   # payments is flaky
    return {
        "service": svc,
        "endpoint": random.choice(ENDPOINTS),
        "status_code": 503 if bad or random.random() < 0.01 else 200,
        "latency_ms": round(random.lognormvariate(4.5, 0.6), 1),
    }


with httpx.Client(base_url=BASE, timeout=30) as c:
    start = time.perf_counter()
    for _ in range(TOTAL // BATCH):
        c.post("/events", json=[make_event() for _ in range(BATCH)]).raise_for_status()
    elapsed = time.perf_counter() - start
    for s in SERVICES:
        c.post("/heartbeats", json={"service": s, "up": True})
    c.post("/alert-rules", json={"service": "payments", "metric": "error_rate",
                                 "operator": ">", "threshold": 5, "severity": "critical"})
    c.post("/alert-rules", json={"service": "checkout", "metric": "p95_latency_ms",
                                 "operator": ">", "threshold": 500, "severity": "warning"})
    print("evaluate:", c.post("/alerts/evaluate").json())
    print(f"ingested {TOTAL:,} events in {elapsed:.1f}s ({TOTAL/elapsed:,.0f} events/s)")
    t = time.perf_counter()
    c.get("/reports/summary").raise_for_status()
    print(f"summary report over {TOTAL:,} rows: {(time.perf_counter()-t)*1000:.0f} ms")
