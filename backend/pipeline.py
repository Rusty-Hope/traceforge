"""DB orchestration: raw events -> log clusters (incidents) -> scored windows.

Shared by the simulator CLI, the API and the ingest endpoint. Engines live in
module state; analyze_cycle() is serialized with a lock. Windows are aggregated
incrementally (WindowAggregator) — no full rebuilds. Confirmed anomalies get a
blast-radius correlation and, together with p95 drift risk, are published to
the SSE stream.
"""
from __future__ import annotations

import threading
from datetime import timedelta
from typing import Iterable

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_session
from .events import publish
from .ml_engine import (
    AnomalyDetector,
    LogClusterer,
    WindowAggregator,
    feature_vector,
    pearson,
    window_start,
)
from .models import Incident, LogEvent, MetricWindow, utcnow

_lock = threading.Lock()
_clusterer: LogClusterer | None = None
_detectors: dict[str, AnomalyDetector] = {}
_aggregator: WindowAggregator | None = None


def clusterer() -> LogClusterer:
    global _clusterer
    if _clusterer is None:
        _clusterer = LogClusterer(similarity=get_settings().cluster_similarity)
    return _clusterer


def detector_stats() -> dict[str, dict]:
    s = get_settings()
    return {svc: {"fitted": det.fitted, "baseline_size": det.baseline_size,
                  "threshold": round(det.effective_threshold(s.anomaly_threshold), 3)}
            for svc, det in sorted(_detectors.items())}


def reset_engines() -> None:
    """Drop in-memory ML state (tests / re-baselining)."""
    global _clusterer, _aggregator
    _clusterer = None
    _aggregator = None
    _detectors.clear()


def ingest(db: Session, events: Iterable[dict]) -> int:
    """Persist a batch of raw telemetry dicts."""
    rows = [LogEvent(**e) for e in events]
    db.add_all(rows)
    db.commit()
    return len(rows)


def prune_old_events(db: Session | None = None) -> int:
    """Retention: drop raw events past the window; windows/incidents are the durable aggregate."""
    s = get_settings()
    own = db is None
    session = db if db is not None else get_session()
    cutoff = utcnow() - timedelta(hours=s.retention_hours)
    result = session.execute(delete(LogEvent).where(LogEvent.ts < cutoff))
    session.commit()
    if own:
        session.close()
    return result.rowcount or 0


def analyze_cycle(db: Session) -> dict:
    """One full local-ML pass over new telemetry. Cheap: zero network, zero LLM."""
    with _lock:
        return _analyze_cycle(db)


def _analyze_cycle(db: Session) -> dict:
    s = get_settings()
    events = db.scalars(
        select(LogEvent).where(LogEvent.clustered.is_(False)).order_by(LogEvent.id)
    ).all()
    if not events:
        return _summary(db, 0, [], 0, [], [])

    agg = _aggregator_init(db)

    # 1) Cluster raw messages into canonical fingerprints; incident-track ERROR/FATAL.
    clu = clusterer()
    inc_by_fp: dict[str, Incident] = {}
    new_refs: list[Incident] = []
    for e in events:
        a = clu.assign(e.message)
        if not clu.warmed:  # cold-start buffering: retry these events next cycle
            continue
        e.fingerprint, e.clustered = a.fingerprint, True
        if (e.level or "").upper() not in {"ERROR", "FATAL"}:
            continue
        inc = inc_by_fp.get(a.fingerprint) or db.scalar(
            select(Incident).where(Incident.fingerprint == a.fingerprint)
        )
        if inc is None:
            inc = Incident(
                fingerprint=a.fingerprint, service=e.service,
                level=(e.level or "ERROR").upper(), title=a.template[:200],
                example_message=e.message[:500], ml_confidence=a.confidence,
                occurrences=1,  # explicit: column defaults only apply at INSERT, and we may mutate before flush
            )
            db.add(inc)
            new_refs.append(inc)
        else:
            inc.occurrences += 1
            inc.ml_confidence = max(inc.ml_confidence, a.confidence)
            inc.last_seen = utcnow()
        inc_by_fp[a.fingerprint] = inc
    db.flush()
    new_incidents = [
        {"id": inc.id, "service": inc.service, "level": inc.level,
         "title": inc.title[:120], "confidence": round(inc.ml_confidence, 3)}
        for inc in new_refs
    ]
    for inc in new_refs:
        publish("incident_created", {"id": inc.id, "service": inc.service, "title": inc.title[:120]})

    # 2) Incremental window aggregation; score closed windows once.
    now = max(e.ts for e in events)
    window_s = timedelta(seconds=s.window_size_s)
    fps_by_window: dict[tuple[str, datetime], set[str]] = {}
    touched_set: set[tuple[str, datetime]] = set()
    for e in events:
        key = agg.add(e)
        touched_set.add(key)
        if e.fingerprint:
            fps_by_window.setdefault(key, set()).add(e.fingerprint)

    anomalies, drifts = 0, []
    for service, start in sorted(touched_set):
        wf = agg.snapshot(service, start)
        m = db.scalar(select(MetricWindow).where(MetricWindow.service == service, MetricWindow.ts == start))
        if m is None:
            m = MetricWindow(ts=start, service=service)
        for name in ("error_rate", "p95_latency_ms", "mean_latency_ms", "log_entropy",
                     "rps", "frac_2xx", "frac_4xx", "frac_5xx", "contaminated"):
            setattr(m, name, getattr(wf, name))

        if start + window_s <= now and not m.scored:
            det = _detector_for(db, service)
            score = det.observe(feature_vector(wf), wf.contaminated,
                                p95_ms=wf.p95_latency_ms, static_threshold=s.anomaly_threshold)
            m.anomaly_score = round(score, 4) if score is not None else 0.0
            threshold = det.effective_threshold(s.anomaly_threshold)
            m.is_anomaly = score is not None and score >= threshold
            baseline = det.p95_baseline()
            m.forecast_risk = round(
                max(0.0, min(1.0, (wf.p95_latency_ms / max(baseline, 1e-6) - 1.0) / 2.0)), 3
            ) if baseline else 0.0
            m.scored = True
            if m.is_anomaly:
                anomalies += 1
                related = _blast_radius(db, service)
                for fp in fps_by_window.get((service, start), ()):  # bump involved incidents
                    inc = inc_by_fp.get(fp) or db.scalar(select(Incident).where(Incident.fingerprint == fp))
                    if inc is not None:
                        inc.max_anomaly_score = max(inc.max_anomaly_score, m.anomaly_score)
                        if related:
                            inc.blast_radius = {"services": related, "source": service}
                publish("window_anomaly", {
                    "service": service, "ts": start.isoformat(), "score": m.anomaly_score,
                    "forecast_risk": m.forecast_risk, "related": related,
                })
            elif m.forecast_risk >= 0.5:
                drifts.append({"service": service, "risk": m.forecast_risk})
                publish("drift", {"service": service, "risk": m.forecast_risk, "p95_ms": wf.p95_latency_ms})
        db.add(m)

    agg.evict_older_than(now)
    db.commit()
    return _summary(db, len(events), sorted(touched_set), anomalies, drifts, new_incidents)


def _aggregator_init(db: Session) -> WindowAggregator:
    global _aggregator
    if _aggregator is None:
        s = get_settings()
        _aggregator = WindowAggregator(window_s=s.window_size_s)
        since = utcnow() - timedelta(seconds=3 * s.window_size_s)  # warm start after restart
        for e in db.scalars(select(LogEvent).where(LogEvent.ts >= since)):
            _aggregator.add(e)
    return _aggregator


def _detector_for(db: Session, service: str) -> AnomalyDetector:
    det = _detectors.get(service)
    if det is not None:
        return det
    s = get_settings()
    det = AnomalyDetector(contamination=s.contamination, min_train=s.baseline_min_windows,
                          retrain_every=s.retrain_every)
    _detectors[service] = det
    rows = db.scalars(  # warm start from persisted clean windows so restarts skip re-warming
        select(MetricWindow)
        .where(MetricWindow.service == service, MetricWindow.scored.is_(True),
               MetricWindow.contaminated.is_(False))
        .order_by(MetricWindow.ts.desc()).limit(1500)
    ).all()
    for row in reversed(rows):
        det.observe(feature_vector(row), contaminated=False,
                    p95_ms=row.p95_latency_ms, static_threshold=s.anomaly_threshold)
    return det


def _blast_radius(db: Session, service: str, minutes: int = 30) -> list[str]:
    """Services whose recent error_rate correlates with the flagged one."""
    since = utcnow() - timedelta(minutes=minutes)
    rows = db.execute(
        select(MetricWindow.service, MetricWindow.ts, MetricWindow.error_rate)
        .where(MetricWindow.ts >= since)
    ).all()
    per_service: dict[str, dict[str, float]] = {}
    for svc, ts, error_rate in rows:
        per_service.setdefault(svc, {})[ts.isoformat()] = error_rate
    base = per_service.get(service) or {}
    related = []
    for svc, series in per_service.items():
        if svc == service:
            continue
        common = sorted(set(base) & set(series))
        if len(common) < 8:
            continue
        r = pearson([base[t] for t in common], [series[t] for t in common])
        peak = max(series[t] for t in common)
        if r >= 0.6 and peak > 0.05:
            related.append(svc)
    return related


def _incident_count(db: Session) -> int:
    return int(db.scalar(select(func.count()).select_from(Incident)) or 0)


def _summary(db: Session, events: int, windows: list, anomalies: int,
             drifts: list[dict], new_incidents: list[dict]) -> dict:
    return {"events": events, "windows": len(windows), "anomalies": anomalies,
            "drifts": drifts, "incident_count": _incident_count(db),
            "new_incidents": new_incidents, "detectors": detector_stats()}
