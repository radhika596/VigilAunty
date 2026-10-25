import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.metrics import percentile


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VIGILAUNTY_DB", str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def ev(service="api", endpoint="/users", status=200, latency=100.0):
    return {"service": service, "endpoint": endpoint,
            "status_code": status, "latency_ms": latency}


def test_percentile_nearest_rank():
    vals = list(range(1, 101))
    assert percentile(vals, 50) == 50
    assert percentile(vals, 95) == 95
    assert percentile(vals, 99) == 99
    assert percentile([], 50) is None


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_ingest_and_metrics(client):
    batch = [ev(latency=float(i)) for i in range(1, 100)] + [ev(status=500, latency=100.0)]
    r = client.post("/events", json=batch)
    assert r.status_code == 202 and r.json()["accepted"] == 100

    m = client.get("/services/api/metrics").json()
    assert m["request_count"] == 100
    assert m["error_count"] == 1
    assert m["error_rate"] == 1.0
    assert m["p50_latency_ms"] == 50.0
    assert m["p99_latency_ms"] == 99.0
    assert m["availability"] is None


def test_availability_from_heartbeats(client):
    for up in (True, True, True, False):
        client.post("/heartbeats", json={"service": "api", "up": up})
    assert client.get("/services/api/metrics").json()["availability"] == 75.0


def test_unknown_service_404(client):
    assert client.get("/services/nope/metrics").status_code == 404
    rule = {"service": "nope", "metric": "error_rate", "operator": ">", "threshold": 5}
    assert client.post("/alert-rules", json=rule).status_code == 404


def test_validation_rejects_bad_input(client):
    assert client.post("/events", json=[ev(status=42)]).status_code == 422
    client.post("/events", json=[ev()])
    bad = {"service": "api", "metric": "cpu", "operator": ">", "threshold": 1}
    assert client.post("/alert-rules", json=bad).status_code == 422


def test_incident_lifecycle(client):
    client.post("/events", json=[ev(status=500), ev(status=500)])
    rule = {"service": "api", "metric": "error_rate", "operator": ">",
            "threshold": 50, "window_minutes": 10, "severity": "critical"}
    assert client.post("/alert-rules", json=rule).status_code == 201

    first = client.post("/alerts/evaluate").json()
    assert len(first["opened"]) == 1

    # sustained breach must not open a duplicate incident
    assert client.post("/alerts/evaluate").json()["opened"] == []
    assert len(client.get("/incidents?status=open").json()) == 1
    assert client.get("/reports/summary").json()["total_open_incidents"] == 1

    # recovery resolves it
    client.post("/events", json=[ev() for _ in range(10)])
    result = client.post("/alerts/evaluate").json()
    assert len(result["resolved"]) == 1
    assert client.get("/incidents?status=open").json() == []
    assert len(client.get("/incidents?status=resolved").json()) == 1


def test_endpoint_breakdown(client):
    client.post("/events", json=[ev(endpoint="/slow", latency=900), ev(endpoint="/fast", latency=10)])
    rows = client.get("/services/api/endpoints").json()
    assert rows[0]["endpoint"] == "/slow"
