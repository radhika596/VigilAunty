Claude














Main · PY
import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Optional
 
from pathlib import Path
 
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
 
from . import alerts, db
from .logging_config import configure_logging
from .metrics import METRIC_NAMES, service_metrics
 
log = logging.getLogger("vigilaunty")
 
 
# ---------- schemas ----------
class EventIn(BaseModel):
    service: str = Field(min_length=1, max_length=100)
    endpoint: str = Field(min_length=1, max_length=300)
    status_code: int = Field(ge=100, le=599)
    latency_ms: float = Field(ge=0)
    ts: Optional[float] = None
 
 
class HeartbeatIn(BaseModel):
    service: str = Field(min_length=1, max_length=100)
    up: bool
    ts: Optional[float] = None
 
 
class RuleIn(BaseModel):
    service: str
    metric: Literal[METRIC_NAMES]  # type: ignore[valid-type]
    operator: Literal[">", ">=", "<", "<="]
    threshold: float
    window_minutes: int = Field(default=5, ge=1, le=1440)
    severity: Literal["info", "warning", "critical"] = "warning"
 
 
# ---------- app ----------
def _run_evaluation() -> dict:
    with db.connect() as conn:
        return alerts.evaluate_rules(conn)
 
 
async def _evaluator_loop(interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_run_evaluation)
        except Exception:
            log.exception("background evaluation failed")
 
 
@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    db.init_db()
    interval = float(os.environ.get("EVALUATE_INTERVAL_SECONDS", "0"))
    task = asyncio.create_task(_evaluator_loop(interval)) if interval > 0 else None
    yield
    if task:
        task.cancel()
 
 
app = FastAPI(title="Vigilaunty", version="1.0.0", lifespan=lifespan)
 
 
@app.middleware("http")
async def access_log(request: Request, call_next):
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        log.info("request", extra={"fields": {
            "method": request.method, "path": request.url.path,
            "status": status,
            "duration_ms": round((time.perf_counter() - start) * 1000, 2),
        }})
 
 
def _service_id_or_404(conn, name: str) -> int:
    row = conn.execute("SELECT id FROM services WHERE name = ?", (name,)).fetchone()
    if not row:
        raise HTTPException(404, f"unknown service '{name}'")
    return row["id"]
 
 
# ---------- ingestion ----------
@app.get("/health")
def health():
    return {"status": "ok"}
 
 
@app.post("/events", status_code=202)
def ingest_events(events: Annotated[list[EventIn], Body(max_length=1000)]):
    now = time.time()
    with db.connect() as conn:
        ids: dict[str, int] = {}
        rows = []
        for e in events:
            if e.service not in ids:
                ids[e.service] = db.get_or_create_service(conn, e.service)
            rows.append((ids[e.service], e.endpoint, e.status_code, e.latency_ms, e.ts or now))
        conn.executemany(
            "INSERT INTO events (service_id, endpoint, status_code, latency_ms, ts) "
            "VALUES (?, ?, ?, ?, ?)", rows,
        )
    return {"accepted": len(rows)}
 
 
@app.post("/heartbeats", status_code=202)
def ingest_heartbeat(hb: HeartbeatIn):
    with db.connect() as conn:
        sid = db.get_or_create_service(conn, hb.service)
        conn.execute(
            "INSERT INTO heartbeats (service_id, up, ts) VALUES (?, ?, ?)",
            (sid, int(hb.up), hb.ts or time.time()),
        )
    return {"accepted": 1}
 
 
# ---------- querying ----------
@app.get("/services")
def list_services():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM services ORDER BY name")]
 
 
@app.get("/services/{name}/metrics")
def get_metrics(name: str, window_minutes: int = Query(60, ge=1, le=1440)):
    with db.connect() as conn:
        sid = _service_id_or_404(conn, name)
        return {"service": name, **service_metrics(conn, sid, window_minutes)}
 
 
@app.get("/services/{name}/endpoints")
def slowest_endpoints(name: str, window_minutes: int = Query(60, ge=1, le=1440),
                      limit: int = Query(5, ge=1, le=50)):
    """Per-endpoint breakdown, handy when narrowing down an incident."""
    since = time.time() - window_minutes * 60
    with db.connect() as conn:
        sid = _service_id_or_404(conn, name)
        rows = conn.execute(
            "SELECT endpoint, COUNT(*) AS requests, "
            "ROUND(AVG(latency_ms), 2) AS avg_latency_ms, "
            "SUM(status_code >= 500) AS errors "
            "FROM events WHERE service_id = ? AND ts >= ? "
            "GROUP BY endpoint ORDER BY avg_latency_ms DESC LIMIT ?",
            (sid, since, limit),
        ).fetchall()
        return [dict(r) for r in rows]
 
 
# ---------- alerting ----------
@app.post("/alert-rules", status_code=201)
def create_rule(rule: RuleIn):
    with db.connect() as conn:
        sid = _service_id_or_404(conn, rule.service)
        cur = conn.execute(
            "INSERT INTO alert_rules (service_id, metric, operator, threshold, "
            "window_minutes, severity, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, rule.metric, rule.operator, rule.threshold,
             rule.window_minutes, rule.severity, time.time()),
        )
        return {"id": cur.lastrowid, **rule.model_dump()}
 
 
@app.get("/alert-rules")
def list_rules():
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT r.id, s.name AS service, r.metric, r.operator, r.threshold, "
            "r.window_minutes, r.severity FROM alert_rules r "
            "JOIN services s ON s.id = r.service_id ORDER BY r.id")]
 
 
@app.post("/alerts/evaluate")
def evaluate_now():
    return _run_evaluation()
 
 
@app.get("/incidents")
def list_incidents(status: Literal["open", "resolved", "all"] = "all"):
    where = {"open": "WHERE i.resolved_at IS NULL",
             "resolved": "WHERE i.resolved_at IS NOT NULL", "all": ""}[status]
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT i.id, s.name AS service, r.metric, r.operator, r.threshold, "
            "r.severity, i.opened_at, i.resolved_at, i.peak_value, i.last_value "
            "FROM incidents i JOIN alert_rules r ON r.id = i.rule_id "
            f"JOIN services s ON s.id = r.service_id {where} "
            "ORDER BY i.opened_at DESC")
        return [dict(r) for r in rows]
 
 
# ---------- dashboard report ----------
@app.get("/reports/summary")
def summary(window_minutes: int = Query(60, ge=1, le=1440)):
    with db.connect() as conn:
        services = conn.execute("SELECT id, name FROM services ORDER BY name").fetchall()
        report = []
        for s in services:
            open_count = conn.execute(
                "SELECT COUNT(*) FROM incidents i JOIN alert_rules r ON r.id = i.rule_id "
                "WHERE r.service_id = ? AND i.resolved_at IS NULL", (s["id"],)
            ).fetchone()[0]
            report.append({
                "service": s["name"], "open_incidents": open_count,
                **service_metrics(conn, s["id"], window_minutes),
            })
        return {
            "generated_at": time.time(),
            "window_minutes": window_minutes,
            "services": report,
            "total_open_incidents": sum(r["open_incidents"] for r in report),
        }
 
 
# ---------- dashboard UI ----------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return (Path(__file__).parent / "static" / "dashboard.html").read_text(encoding="utf-8")
 

