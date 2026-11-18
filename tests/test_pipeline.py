"""Integration tests: pipeline orchestration on a real SQLite DB."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from backend.models import Incident, LogEvent, MetricWindow
from backend.pipeline import analyze_cycle, clusterer, ingest, reset_engines, warm_engines

BASE = datetime(2026, 1, 1, 0, 0, 0)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
CLEAN_TEMPLATES = [
    "GET /v1/orders 200 40ms upstream=orders-service trace=aaa111bbb222",
    "order created id=ord_aaa111bbb222 items=3 total=$42.00",
    "stock reserved sku=SKU-42 qty=1",
]


def _add_clean(db, ts, service="orders-service", lat=40.0):
    msg = CLEAN_TEMPLATES[int(ts.timestamp()) % len(CLEAN_TEMPLATES)]
    db.add(LogEvent(ts=ts, service=service, level="INFO", status_code=200,
                    latency_ms=lat, message=msg))


def test_error_cluster_becomes_single_incident_across_batch(db):
    msgs = [
        "err one id=aaaa1111bbbb", "err two id=cccc2222dddd", "err three id=eeee3333ffff",
        "err one id=aaaa1111bbbb", "err one id=aaaa1111bbbb",
    ]
    now = _now()
    for m in msgs:
        db.add(LogEvent(ts=now, service="payments-service", level="ERROR", message=m))
    db.commit()

    analyze_cycle(db)          # cold-start cycle (buffers templates)
    summary = analyze_cycle(db)

    assert summary["incident_count"] == 3
    recurring = db.scalar(select(Incident).where(Incident.title.like("err one%")))
    assert recurring.occurrences == 3
    assert recurring.triage_state == "pending"


def test_cycle_is_idempotent(db):
    now = _now()
    db.add(LogEvent(ts=now, service="payments-service", level="ERROR", message="boom id=aaaa1111bbbb"))
    db.commit()
    analyze_cycle(db)
    analyze_cycle(db)
    s3 = analyze_cycle(db)
    assert s3["events"] == 0
    assert s3["incident_count"] == 1
    inc = db.scalar(select(Incident))
    assert inc.occurrences == 1  # no double counting on empty cycles


def test_info_events_cluster_but_never_become_incidents(db):
    now = _now()
    db.add(LogEvent(ts=now, service="orders-service", level="INFO",
                    message="order created id=ord_aaa111bbb222 items=3 total=$42.00"))
    db.commit()
    analyze_cycle(db)
    analyze_cycle(db)
    assert db.scalar(select(Incident)) is None


def test_extreme_window_is_flagged_and_score_propagates_to_incident(db):
    for i in range(12):  # 12 clean windows with realistic latency variance
        t = BASE + timedelta(seconds=2 * i)
        _add_clean(db, t + timedelta(milliseconds=100), lat=35.0 + (i * 13) % 23)
    t = BASE + timedelta(seconds=24)  # extreme window
    for k in range(6):
        db.add(LogEvent(ts=t + timedelta(milliseconds=100 * k), service="orders-service",
                        level="ERROR", status_code=500, latency_ms=800.0,
                        message="sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection pool exhausted, timeout: 30000 ms"))
    for i in range(2):  # tail traffic so the extreme window actually closes (start+2s <= now)
        _add_clean(db, BASE + timedelta(seconds=26 + 2 * i, milliseconds=100),
                   lat=35.0 + (i * 13) % 23)
    db.commit()

    for _ in range(4):
        analyze_cycle(db)

    flagged = [r for r in db.scalars(select(MetricWindow)).all() if r.is_anomaly]
    assert flagged, "the extreme window must be flagged by the IsolationForest"
    inc = db.scalar(select(Incident).where(Incident.title.contains("pool exhausted")))
    assert inc is not None
    assert inc.max_anomaly_score >= 0.62


def test_ingest_persists_and_analyzes(db):
    aware_ts = datetime(2026, 2, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    n = ingest(db, [
        {"ts": aware_ts, "service": "orders-service",
         "level": "ERROR", "message": "payment declined id=ord_aaa111bbb222 reason=insufficient_funds"},
    ])
    assert n == 1
    summary = analyze_cycle(db)
    assert summary["events"] == 1
    inc = db.scalar(select(Incident))
    assert inc.first_seen == datetime(2026, 2, 1, 10, 0)
    assert inc.last_seen == datetime(2026, 2, 1, 10, 0)


def test_cluster_merge_consolidates_persisted_incidents(db):
    clu = clusterer()
    clu.similarity = 0.99  # force two near-duplicate templates to fork initially
    clu.refit_every = 10_000
    t0 = datetime(2026, 1, 1, 1, 0, 0)
    ingest(db, [
        {"ts": t0, "service": "postgres-primary", "level": "INFO",
         "message": "totally unrelated kernel panic at ring zero"},
        {"ts": t0, "service": "orders-service", "level": "ERROR",
         "message": "database pool exhausted timeout 30000 ms service one alpha beta gamma delta"},
        {"ts": t0 + timedelta(seconds=1), "service": "orders-service", "level": "ERROR",
         "message": "database pool exhausted timeout 30000 ms service two alpha beta gamma delta"},
    ])
    analyze_cycle(db)
    assert len(db.scalars(select(Incident)).all()) == 2

    clu._refit()
    ingest(db, [
        {"ts": t0 + timedelta(seconds=2), "service": "orders-service", "level": "ERROR",
         "message": "database pool exhausted timeout 30000 ms service one alpha beta gamma delta"},
    ])
    analyze_cycle(db)
    incidents = db.scalars(select(Incident)).all()
    assert len(incidents) == 1
    assert incidents[0].occurrences == 3


def test_warm_engines_restores_clusterer_after_restart(db):
    t0 = _now()
    ingest(db, [
        {"ts": t0, "service": "auth-service", "level": "ERROR",
         "message": "keyring backend unavailable, falling back to env provider"},
    ])
    analyze_cycle(db)
    original_fp = db.scalar(select(Incident)).fingerprint

    reset_engines()
    assert not clusterer().warmed
    warm_engines(db)
    assert clusterer().warmed
    assert clusterer().cluster_count == 1
    assert clusterer().assign("keyring backend unavailable, falling back to env provider").fingerprint == original_fp


def test_blast_radius_correlates_historical_windows(db):
    for i in range(12):
        t = BASE + timedelta(seconds=2 * i)
        is_err = i >= 9
        for svc in ("postgres-primary", "orders-service"):
            db.add(LogEvent(
                ts=t + timedelta(milliseconds=100),
                service=svc,
                level="ERROR" if is_err else "INFO",
                status_code=500 if is_err else 200,
                latency_ms=800.0 if is_err else 20.0,
                message="pool exhausted id=aaaa1111bbbb" if is_err else CLEAN_TEMPLATES[i % len(CLEAN_TEMPLATES)],
            ))
    # Tail event to close the last anomalous window
    for svc in ("postgres-primary", "orders-service"):
        _add_clean(db, BASE + timedelta(seconds=24), service=svc, lat=20.0)
    db.commit()

    analyze_cycle(db)
    inc = db.scalar(select(Incident).where(Incident.title.contains("pool exhausted")))
    assert inc is not None
    assert inc.blast_radius is not None
    assert "orders-service" in inc.blast_radius["services"] or "postgres-primary" in inc.blast_radius["services"]
