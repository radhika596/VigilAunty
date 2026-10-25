# Vigilaunty — Application Monitoring and Incident Analytics

FastAPI service that ingests request events and health checks, computes
error rate / p50-p95-p99 latency / availability per service, evaluates
configurable alert rules, and tracks incidents from open to resolved.

**Stack:** Python 3.12, FastAPI, SQLite (SQL), Docker, GitHub Actions

## Run
```bash
docker compose up --build          # http://localhost:8000/docs
# or locally
pip install -r requirements.txt && uvicorn app.main:app --reload
```

## Try it
```bash
curl -X POST localhost:8000/events -H 'content-type: application/json' \
  -d '[{"service":"api","endpoint":"/users","status_code":500,"latency_ms":820}]'
curl -X POST localhost:8000/alert-rules -H 'content-type: application/json' \
  -d '{"service":"api","metric":"error_rate","operator":">","threshold":5,"severity":"critical"}'
curl -X POST localhost:8000/alerts/evaluate
curl localhost:8000/reports/summary
```

## API
| Endpoint | Purpose |
|---|---|
| `POST /events` | Batch ingest (≤1000) request events; services auto-register |
| `POST /heartbeats` | Record up/down checks for availability |
| `GET /services/{name}/metrics?window_minutes=` | Error rate, p50/p95/p99, availability |
| `GET /services/{name}/endpoints` | Per-endpoint breakdown for incident triage |
| `POST/GET /alert-rules` | Metric, operator, threshold, window, severity |
| `POST /alerts/evaluate` | Evaluate rules now (also runs on a timer) |
| `GET /incidents?status=open` | Incident history with peak/last values |
| `GET /reports/summary` | Dashboard-ready JSON across all services |

## Design notes
- **Schema:** `events` and `heartbeats` indexed on `(service_id, ts)` for fast windowed queries.
- **Percentiles:** nearest-rank on latencies sorted in SQL.
- **Alerting:** one open incident per rule (no duplicate spam); auto-resolves on recovery; rules skip windows with no data.
- **Logging:** every request and alert transition emits a JSON log line.
- **Config:** `VIGILAUNTY_DB`, `LOG_LEVEL`, `EVALUATE_INTERVAL_SECONDS` (0 disables the background evaluator).

## Test and measure
```bash
pip install -r requirements-dev.txt
pytest --cov=app
python scripts/load_test.py http://localhost:8000 50000
```
CI (`.github/workflows/ci.yml`) runs tests with an 85% coverage gate, then builds the Docker image.
