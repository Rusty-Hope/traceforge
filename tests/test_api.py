"""FastAPI HTTP integration tests — ingestion, auth, incident workflow, simulation."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.config import get_settings
from backend.main import app


def test_health_engines_services_and_timeseries(db):
    with TestClient(app) as client:
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        eng = client.get("/api/engines")
        assert eng.status_code == 200
        assert "clusterer" in eng.json()

        svcs = client.get("/api/services")
        assert svcs.status_code == 200
        assert len(svcs.json()) >= 7

        ts = client.get("/api/metrics/timeseries?minutes=60")
        assert ts.status_code == 200
        assert isinstance(ts.json(), list)


def test_ingest_token_auth_and_incident_lifecycle(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "ingest_token", "secret-token")
    t0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)
    batch = [
        {"ts": t0.isoformat(), "service": "checkout-api", "level": "ERROR",
         "status_code": 500, "latency_ms": 812.0,
         "message": "psycopg2.OperationalError: pool exhausted timeout=30000 ms"},
        {"ts": (t0 + timedelta(seconds=1)).isoformat(), "service": "checkout-api", "level": "ERROR",
         "status_code": 500, "latency_ms": 910.0,
         "message": "psycopg2.OperationalError: pool exhausted timeout=30000 ms"},
    ]

    with TestClient(app) as client:
        unauth = client.post("/api/ingest", json=batch)
        assert unauth.status_code == 401

        ok = client.post("/api/ingest", json=batch, headers={"X-Ingest-Token": "secret-token"})
        assert ok.status_code == 200
        assert ok.json()["ingested"] == 2

        # External service automatically appears in /api/services
        svc_names = [s["service"] for s in client.get("/api/services").json()]
        assert "checkout-api" in svc_names

        incidents = client.get("/api/incidents").json()
        assert len(incidents) == 1
        inc_id = incidents[0]["id"]
        assert incidents[0]["occurrences"] == 2

        detail = client.get(f"/api/incidents/{inc_id}").json()
        assert len(detail["samples"]) == 2

        ack = client.post(f"/api/incidents/{inc_id}/status", json={"status": "acknowledged"})
        assert ack.status_code == 200
        assert ack.json()["status"] == "acknowledged"

        fb = client.post(f"/api/incidents/{inc_id}/feedback", json={"verdict": "confirmed"})
        assert fb.status_code == 200
        assert fb.json()["feedback"]["verdict"] == "confirmed"

        notes = client.post(f"/api/incidents/{inc_id}/notes", json={"notes": "Raised pool size to 40"})
        assert notes.status_code == 200
        assert notes.json()["resolution_notes"] == "Raised pool size to 40"

        res = client.post(f"/api/incidents/{inc_id}/status", json={"status": "resolved"})
        assert res.status_code == 200
        assert res.json()["resolved_at"] is not None

        pm = client.get(f"/api/incidents/{inc_id}/postmortem")
        assert pm.status_code == 200
        assert "Raised pool size to 40" in pm.text


def test_simulate_incident_fast_forward_and_validation(db):
    with TestClient(app) as client:
        bad = client.post("/api/simulate/incident", json={"kind": "latency_spike", "service": "unknown-svc"})
        assert bad.status_code == 422

        ok = client.post("/api/simulate/incident", json={
            "kind": "cascade_failure",
            "ticks": 12,
            "intensity": 1.5,
            "fast_forward": True,
        })
        assert ok.status_code == 200
        data = ok.json()
        assert data["fast_forwarded"] is True
        assert data["events_generated"] > 0
        assert len(data["new_incidents"]) > 0
