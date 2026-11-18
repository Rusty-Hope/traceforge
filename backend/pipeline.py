"""DB orchestration: raw events -> log clusters (incidents) -> scored windows.

Shared by the simulator CLI, the API and the ingest endpoint. Engines live in
module state; analyze_cycle() is serialized with a lock. Windows are aggregated
incrementally (WindowAggregator) — no full rebuilds. Confirmed anomalies get a
blast-radius correlation and, together with p95 drift risk, are published to
the SSE stream.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Iterable

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_session
from .events import publish
from .ml_engine import (
    AnomalyDetector,
    LogClusterer,
    WindowAggregator,
    feature_vector,
    naive_utc,
    pearson,
    window_start,
)
from .models import Incident, LogEvent, MetricWindow, utcnow

_lock = threading.Lock()
_clusterer: LogClusterer | None = None
_clusterer_hydrated = False
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
    global _clusterer, _clusterer_hydrated, _aggregator
    _clusterer = None
    _clusterer_hydrated = False
    _aggregator = None
    _detectors.clear()


def warm_engines(db: Session) -> None:
    """Hydrate clusterer, window aggregator, and per-service detectors from persisted SQLite state."""
    with _lock:
        _clusterer_init(db)
        _aggregator_init(db)
        services = db.scalars(select(MetricWindow.service).distinct()).all()
        for svc in services:
            _detector_for(db, svc)


def ingest(db: Session, events: Iterable[dict]) -> int:
    """Persist a batch of raw telemetry dicts (normalizing timestamps to naive UTC)."""
    rows = []
    for e in events:
        data = dict(e)
        if data.get("ts") is not None:
            data["ts"] = naive_utc(data["ts"])
        rows.append(LogEvent(**data))
    db.add_all(rows)
    db.commit()
    return len(rows)


def prune_old_events(db: Session | None = None) -> int:
    """Retention: drop raw events past the window; windows/incidents are the durable aggregate."""
    s = get_settings()
    own = db is None
    session = db if db is not None else get_session()
    try:
        latest_ts = session.scalar(select(func.max(LogEvent.ts)))
        ref = max(utcnow(), latest_ts) if latest_ts is not None else utcnow()
        cutoff = ref - timedelta(hours=s.retention_hours)
        result = session.execute(delete(LogEvent).where(LogEvent.ts < cutoff))
        session.commit()
        return result.rowcount or 0
    finally:
        if own:
            session.close()


def analyze_cycle(db: Session) -> dict:
    """One full local-ML pass over new telemetry. Cheap: zero network, zero LLM."""
    with _lock:
        return _analyze_cycle(db)


def _clusterer_init(db: Session) -> LogClusterer:
    global _clusterer_hydrated
    clu = clusterer()
    if not _clusterer_hydrated:
        _clusterer_hydrated = True
        if not clu.warmed:
            clusters = db.execute(
                select(Incident.fingerprint, Incident.title, Incident.occurrences)
            ).all()
            recent_msgs = db.scalars(
                select(LogEvent.message)
                .where(LogEvent.clustered.is_(True))
                .order_by(LogEvent.id.desc())
                .limit(clu.max_corpus)
            ).all()
            if clusters or recent_msgs:
                clu.warm_start(reversed(recent_msgs), clusters)
    return clu


def _apply_cluster_merges(
    db: Session,
    merges: list[tuple[str, str]],
    events: list[LogEvent],
    inc_by_fp: dict[str, Incident],
    new_refs: list[Incident],
) -> None:
    """Sync in-memory LogClusterer merges back to persisted Incident and LogEvent rows."""
    for keep_fp, drop_fp in merges:
        for ev in events:
            if ev.fingerprint == drop_fp:
                ev.fingerprint = keep_fp
        db.execute(update(LogEvent).where(LogEvent.fingerprint == drop_fp).values(fingerprint=keep_fp))

        drop_inc = inc_by_fp.pop(drop_fp, None) or db.scalar(
            select(Incident).where(Incident.fingerprint == drop_fp)
        )
        if drop_inc is None:
            continue
        keep_inc = inc_by_fp.get(keep_fp) or db.scalar(
            select(Incident).where(Incident.fingerprint == keep_fp)
        )
        if keep_inc is None:
            drop_inc.fingerprint = keep_fp
            inc_by_fp[keep_fp] = drop_inc
            continue
        if keep_inc is drop_inc:
            inc_by_fp[keep_fp] = keep_inc
            continue

        keep_inc.occurrences += drop_inc.occurrences
        keep_inc.ml_confidence = max(keep_inc.ml_confidence, drop_inc.ml_confidence)
        keep_inc.max_anomaly_score = max(keep_inc.max_anomaly_score, drop_inc.max_anomaly_score)
        keep_inc.first_seen = min(keep_inc.first_seen, drop_inc.first_seen)
        keep_inc.last_seen = max(keep_inc.last_seen, drop_inc.last_seen)
        if keep_inc.triage is None and drop_inc.triage is not None:
            keep_inc.triage = drop_inc.triage
            keep_inc.severity = drop_inc.severity
            keep_inc.root_cause_component = drop_inc.root_cause_component
            keep_inc.triage_state = drop_inc.triage_state
        if keep_inc.feedback is None and drop_inc.feedback is not None:
            keep_inc.feedback = drop_inc.feedback
        if not keep_inc.resolution_notes and drop_inc.resolution_notes:
            keep_inc.resolution_notes = drop_inc.resolution_notes
        if drop_inc in new_refs:
            new_refs.remove(drop_inc)
        inc_by_fp[keep_fp] = keep_inc
        db.delete(drop_inc)
        db.flush()


def _analyze_cycle(db: Session) -> dict:
    s = get_settings()
    events = db.scalars(
        select(LogEvent).where(LogEvent.clustered.is_(False)).order_by(LogEvent.id)
    ).all()
    if not events:
        return _summary(db, 0, [], 0, [], [])

    agg = _aggregator_init(db)

    # 1) Cluster raw messages into canonical fingerprints; incident-track ERROR/FATAL.
    clu = _clusterer_init(db)
    inc_by_fp: dict[str, Incident] = {}
    new_refs: list[Incident] = []
    for e in events:
        a = clu.assign(e.message)
        if not clu.warmed:  # cold-start buffering: retry these events next cycle
            continue
        e.fingerprint, e.clustered = a.fingerprint, True
        if (e.level or "").upper() not in {"ERROR", "FATAL"}:
            continue
        ev_ts = naive_utc(e.ts) if e.ts else utcnow()
        inc = inc_by_fp.get(a.fingerprint) or db.scalar(
            select(Incident).where(Incident.fingerprint == a.fingerprint)
        )
        if inc is None:
            inc = Incident(
                fingerprint=a.fingerprint, service=e.service,
                level=(e.level or "ERROR").upper(), title=a.template[:200],
                example_message=e.message[:500], ml_confidence=a.confidence,
                occurrences=1,  # explicit: column defaults only apply at INSERT, and we may mutate before flush
                first_seen=ev_ts,
                last_seen=ev_ts,
            )
            db.add(inc)
            new_refs.append(inc)
        else:
            inc.occurrences += 1
            inc.ml_confidence = max(inc.ml_confidence, a.confidence)
            inc.first_seen = min(inc.first_seen, ev_ts)
            inc.last_seen = max(inc.last_seen, ev_ts)
        inc_by_fp[a.fingerprint] = inc
    db.flush()
    merges = clu.drain_merges()
    if merges:
        _apply_cluster_merges(db, merges, events, inc_by_fp, new_refs)
        db.flush()
    new_incidents = [
        {"id": inc.id, "service": inc.service, "level": inc.level,
         "title": inc.title[:120], "confidence": round(inc.ml_confidence, 3)}
        for inc in new_refs
    ]
    for inc in new_refs:
        publish("incident_created", {"id": inc.id, "service": inc.service, "title": inc.title[:120]})

    # 2) Incremental window aggregation; score closed windows once.
    now = max(naive_utc(e.ts) for e in events)
    window_s = timedelta(seconds=s.window_size_s)
    fps_by_window: dict[tuple[str, datetime], set[str]] = {}
    touched_set: set[tuple[str, datetime]] = set()
    for e in events:
        key = agg.add(e)
        touched_set.add(key)
        if e.fingerprint:
            fps_by_window.setdefault(key, set()).add(e.fingerprint)

    anomalies, drifts = 0, []
    window_rows: list[tuple[str, datetime, Any, MetricWindow]] = []
    for service, start in sorted(touched_set, key=lambda item: (item[1], item[0])):
        wf = agg.snapshot(service, start)
        m = db.scalar(select(MetricWindow).where(MetricWindow.service == service, MetricWindow.ts == start))
        if m is None:
            m = MetricWindow(ts=start, service=service)
            db.add(m)
        for name in ("error_rate", "p95_latency_ms", "mean_latency_ms", "log_entropy",
                     "rps", "frac_2xx", "frac_4xx", "frac_5xx", "contaminated"):
            setattr(m, name, getattr(wf, name))
        window_rows.append((service, start, wf, m))
    db.flush()  # ensure all services' window error_rates are visible to _blast_radius

    for service, start, wf, m in window_rows:
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
                related = _blast_radius(db, service, ref_ts=start)
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

    agg.evict_older_than(now)
    db.commit()
    return _summary(db, len(events), sorted(touched_set), anomalies, drifts, new_incidents)


def _aggregator_init(db: Session) -> WindowAggregator:
    global _aggregator
    if _aggregator is None:
        s = get_settings()
        _aggregator = WindowAggregator(window_s=s.window_size_s)
        latest = db.scalar(select(func.max(LogEvent.ts)))
        ref = latest or utcnow()
        since = ref - timedelta(seconds=3 * s.window_size_s)  # warm start after restart
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


def _blast_radius(db: Session, service: str, ref_ts: datetime | None = None, minutes: int = 30) -> list[str]:
    """Services whose recent error_rate correlates with the flagged one."""
    anchor = ref_ts or utcnow()
    since = anchor - timedelta(minutes=minutes)
    rows = db.execute(
        select(MetricWindow.service, MetricWindow.ts, MetricWindow.error_rate)
        .where(MetricWindow.ts >= since, MetricWindow.ts <= anchor)
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
