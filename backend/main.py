"""TraceForge API — local ML on the hot path; the LLM is strictly a background worker.

No chat surface, no prompt input: the triage worker consumes only confirmed,
deduplicated anomaly clusters and writes structured JSON back to SQLite.
External telemetry can enter via POST /api/ingest (token-gated).
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import events
from .config import get_settings
from .database import get_db, get_session, init_db
from .llm_triage import cache_key, llm_active, run_triage_sweep
from .models import Incident, LLMCache, LogEvent, MetricWindow, utcnow
from .pipeline import analyze_cycle, clusterer, detector_stats, ingest, prune_old_events
from .reporting import render_postmortem
from .schemas import (
    EngineStatus,
    FeedbackIn,
    HealthOut,
    IncidentDetail,
    IncidentOut,
    IngestEvent,
    IngestResponse,
    InjectRequest,
    InjectResponse,
    NotesIn,
    ServiceSummary,
    StatusUpdate,
    WindowPoint,
)
from .simulator import SERVICES, TelemetrySimulator

log = logging.getLogger("traceforge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")

SSE_TICK_S = 1.0
RETENTION_SWEEP_S = 900


# ── background loops (the "invisible" part) ──────────────────────────────────
async def _sim_loop(sim: TelemetrySimulator, lock: asyncio.Lock, stop: asyncio.Event) -> None:
    s = get_settings()
    while not stop.is_set():
        async with lock:
            events_batch = sim.tick()
            await asyncio.to_thread(_run_cycle, events_batch)
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(0.05, s.simulate_interval_s))
        except TimeoutError:
            pass


async def _triage_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            summary = await asyncio.to_thread(_triage_sweep)
            if summary.get("processed") or summary.get("cached"):
                log.info("llm triage sweep: %s", summary)
        except Exception:
            log.exception("llm triage sweep failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=get_settings().triage_interval_s)
        except TimeoutError:
            pass


async def _retention_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            pruned = await asyncio.to_thread(prune_old_events)
            if pruned:
                log.info("retention: pruned %d raw events", pruned)
        except Exception:
            log.exception("retention sweep failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=RETENTION_SWEEP_S)
        except TimeoutError:
            pass


def _run_cycle(events_batch: list[dict]) -> dict:
    db = get_session()
    try:
        ingest(db, events_batch)
        return analyze_cycle(db)
    finally:
        db.close()


def _triage_sweep() -> dict:
    db = get_session()
    try:
        return run_triage_sweep(db)
    finally:
        db.close()


def _ingest_batch(payload: list[dict]) -> dict:
    db = get_session()
    try:
        n = ingest(db, payload)
        summary = analyze_cycle(db)
        return {"ingested": n, "analyzed": summary["events"]}
    finally:
        db.close()


def _run_burst(sim: TelemetrySimulator, ticks: int) -> dict:
    total, last = 0, {"events": 0, "new_incidents": []}
    db = get_session()
    try:
        for _ in range(ticks):
            total += ingest(db, sim.tick())
            last = analyze_cycle(db)
    finally:
        db.close()
    return {"events": total, "new_incidents": last["new_incidents"]}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    s = get_settings()
    app.state.simulator = TelemetrySimulator()
    app.state.sim_lock = asyncio.Lock()
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(_triage_loop(stop)),
        asyncio.create_task(_retention_loop(stop)),
    ]
    if s.simulate_enabled:
        tasks.append(asyncio.create_task(
            _sim_loop(app.state.simulator, app.state.sim_lock, stop)))
    log.info("TraceForge up (simulate=%s llm=%s model=%s alerts=%s)",
             s.simulate_enabled, llm_active(), s.llm_model, bool(s.alert_webhook_url))
    yield
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="TraceForge", version="0.4.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _count(db: Session, model, *filters) -> int:
    q = select(func.count()).select_from(model)
    if filters:
        q = q.where(*filters)
    return int(db.scalar(q) or 0)


# ── system ───────────────────────────────────────────────────────────────────
@app.get("/api/health", response_model=HealthOut)
def health():
    s = get_settings()
    return HealthOut(status="ok", simulate_enabled=s.simulate_enabled,
                     llm_active=llm_active(), llm_model=s.llm_model)


@app.get("/api/engines", response_model=EngineStatus)
def engines(db: Session = Depends(get_db)):
    s = get_settings()
    clu = clusterer()
    hour_ago = utcnow() - timedelta(hours=1)
    return EngineStatus(
        clusterer={"warmed": clu.warmed, "clusters": clu.cluster_count, "corpus": clu.corpus_size},
        detectors=detector_stats(),
        llm={
            "enabled": llm_active(),
            "model": s.llm_model,
            "cache_entries": _count(db, LLMCache),
            "cache_hits": int(db.scalar(select(func.coalesce(func.sum(LLMCache.hit_count), 0))) or 0),
            "tokens_prompt": int(db.scalar(select(func.coalesce(func.sum(LLMCache.prompt_tokens), 0))) or 0),
            "tokens_completion": int(db.scalar(select(func.coalesce(func.sum(LLMCache.completion_tokens), 0))) or 0),
        },
        stats={
            "events_total": _count(db, LogEvent),
            "incidents_open": _count(db, Incident, Incident.status != "resolved"),
            "anomalies_1h": _count(db, MetricWindow, MetricWindow.is_anomaly.is_(True),
                                   MetricWindow.ts >= hour_ago),
        },
    )


# ── telemetry ────────────────────────────────────────────────────────────────
@app.get("/api/metrics/timeseries", response_model=list[WindowPoint])
def timeseries(service: str | None = None, minutes: int = 60, db: Session = Depends(get_db)):
    q = select(MetricWindow).where(MetricWindow.ts >= utcnow() - timedelta(minutes=min(minutes, 1440)))
    if service:
        q = q.where(MetricWindow.service == service)
    return db.scalars(q.order_by(MetricWindow.ts).limit(3000)).all()


@app.get("/api/services", response_model=list[ServiceSummary])
def services(db: Session = Depends(get_db)):
    det = detector_stats()
    open_counts = dict(db.execute(
        select(Incident.service, func.count())
        .where(Incident.status != "resolved").group_by(Incident.service)
    ).all())
    out: list[ServiceSummary] = []
    for svc in SERVICES:
        d = det.get(svc, {"fitted": False, "baseline_size": 0, "threshold": 0.0})
        latest = db.scalar(select(MetricWindow).where(MetricWindow.service == svc)
                           .order_by(MetricWindow.ts.desc()).limit(1))
        out.append(ServiceSummary(
            service=svc, detector_fitted=d["fitted"], baseline_size=d["baseline_size"],
            open_incidents=int(open_counts.get(svc, 0)), latest=latest))
    return out


@app.get("/api/stream")
async def stream(request: Request):
    """SSE: live pipeline events (window_anomaly, drift, incident_created, incident_triaged)."""
    async def gen():
        seq = 0
        while True:
            if await request.is_disconnected():
                break
            for item in events.since(seq):
                seq = item["seq"]
                yield f"id: {item['seq']}\nevent: {item['type']}\ndata: {json.dumps(item['data'])}\n\n"
            await asyncio.sleep(SSE_TICK_S)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ── external ingest (Vector / Fluent Bit / your apps) ────────────────────────
@app.post("/api/ingest", response_model=IngestResponse)
async def ingest_events(request: Request, events_batch: list[IngestEvent]):
    s = get_settings()
    if s.ingest_token and request.headers.get("x-ingest-token") != s.ingest_token:
        raise HTTPException(401, "invalid ingest token")
    payload = []
    for e in events_batch:
        d = e.model_dump()
        d["ts"] = d["ts"] or utcnow()
        d["trace_id"] = d["trace_id"] or uuid.uuid4().hex[:16]
        payload.append(d)
    result = await asyncio.to_thread(_ingest_batch, payload)
    return IngestResponse(accepted=len(payload), ingested=result["ingested"], analyzed=result["analyzed"])


# ── incidents ────────────────────────────────────────────────────────────────
@app.get("/api/incidents", response_model=list[IncidentOut])
def list_incidents(status: str | None = None, service: str | None = None,
                   limit: int = 100, db: Session = Depends(get_db)):
    q = select(Incident).order_by(Incident.last_seen.desc()).limit(min(max(limit, 1), 500))
    if status:
        q = q.where(Incident.status == status)
    if service:
        q = q.where(Incident.service == service)
    return db.scalars(q).all()


@app.get("/api/incidents/{incident_id}", response_model=IncidentDetail)
def incident_detail(incident_id: int, db: Session = Depends(get_db)):
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    detail = IncidentDetail.model_validate(inc)
    detail.samples = db.scalars(
        select(LogEvent).where(LogEvent.fingerprint == inc.fingerprint)
        .order_by(LogEvent.ts.desc()).limit(12)
    ).all()
    cache_row = db.get(LLMCache, cache_key(inc.fingerprint))
    if cache_row is not None:
        detail.tokens = {"prompt": cache_row.prompt_tokens, "completion": cache_row.completion_tokens}
    return detail


@app.post("/api/incidents/{incident_id}/status", response_model=IncidentOut)
def set_incident_status(incident_id: int, payload: StatusUpdate, db: Session = Depends(get_db)):
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    inc.status = payload.status
    inc.resolved_at = utcnow() if payload.status == "resolved" else None
    db.commit()
    db.refresh(inc)
    return inc


@app.post("/api/incidents/{incident_id}/feedback", response_model=IncidentOut)
def set_incident_feedback(incident_id: int, payload: FeedbackIn, db: Session = Depends(get_db)):
    """Human verdict on the LLM triage — the accuracy feedback loop."""
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    inc.feedback = {"verdict": payload.verdict, "actual_cause": payload.actual_cause,
                    "at": utcnow().isoformat()}
    db.commit()
    db.refresh(inc)
    return inc


@app.post("/api/incidents/{incident_id}/notes", response_model=IncidentOut)
def set_incident_notes(incident_id: int, payload: NotesIn, db: Session = Depends(get_db)):
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    inc.resolution_notes = payload.notes or None
    db.commit()
    db.refresh(inc)
    return inc


@app.get("/api/incidents/{incident_id}/postmortem")
def postmortem(incident_id: int, db: Session = Depends(get_db)):
    inc = db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, "incident not found")
    return Response(
        render_postmortem(db, inc),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="postmortem-{incident_id}.md"'},
    )


# ── chaos injection (live demos) ─────────────────────────────────────────────
@app.post("/api/simulate/incident", response_model=InjectResponse)
async def inject_incident(payload: InjectRequest):
    sim: TelemetrySimulator = app.state.simulator
    sc = sim.inject(payload.kind, service=payload.service,
                    ticks=payload.ticks, intensity=payload.intensity)
    scenario = {"kind": sc.kind, "service": sc.service, "ticks": sc.total_ticks, "intensity": sc.intensity}
    if payload.fast_forward:
        async with app.state.sim_lock:
            result = await asyncio.to_thread(_run_burst, sim, payload.ticks + 2)
        return InjectResponse(scenario=scenario, fast_forwarded=True,
                              events_generated=result["events"],
                              new_incidents=result["new_incidents"],
                              note="materialized immediately")
    eta = round(sc.total_ticks * get_settings().simulate_interval_s, 1)
    return InjectResponse(scenario=scenario, fast_forwarded=False,
                          note=f"queued; the background stream materializes it in ~{eta}s")


# Serve the built frontend when present — single-binary deployment.
_dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
if _dist.is_dir():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="web")
