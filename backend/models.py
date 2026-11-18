"""SQLite schema (WAL). All timestamps are naive UTC."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LogEvent(Base):
    """Raw structured telemetry line."""

    __tablename__ = "log_events"
    __table_args__ = (Index("ix_log_events_service_ts", "service", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    service: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    status_code: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str] = mapped_column(Text)
    trace_id: Mapped[str] = mapped_column(String(32), default="")
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    clustered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)


class MetricWindow(Base):
    """One sliding-window feature row per (service, window start)."""

    __tablename__ = "metric_windows"
    __table_args__ = (
        UniqueConstraint("service", "ts", name="uq_metric_window"),
        Index("ix_metric_windows_ts", "ts"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime)  # window start
    service: Mapped[str] = mapped_column(String(64))
    error_rate: Mapped[float] = mapped_column(Float, default=0.0)
    p95_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    mean_latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    log_entropy: Mapped[float] = mapped_column(Float, default=0.0)
    rps: Mapped[float] = mapped_column(Float, default=0.0)
    frac_2xx: Mapped[float] = mapped_column(Float, default=0.0)
    frac_4xx: Mapped[float] = mapped_column(Float, default=0.0)
    frac_5xx: Mapped[float] = mapped_column(Float, default=0.0)
    anomaly_score: Mapped[float] = mapped_column(Float, default=0.0)
    is_anomaly: Mapped[bool] = mapped_column(Boolean, default=False)
    contaminated: Mapped[bool] = mapped_column(Boolean, default=False)  # scenario active -> excluded from baseline
    scored: Mapped[bool] = mapped_column(Boolean, default=False)        # passed through the IsolationForest once
    forecast_risk: Mapped[float] = mapped_column(Float, default=0.0)    # p95 drift vs clean baseline, 0..1


class Incident(Base):
    """Canonical incident fingerprint (log cluster) + LLM triage payload."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))                # canonical normalized template
    service: Mapped[str] = mapped_column(String(64), index=True)   # first seen on
    level: Mapped[str] = mapped_column(String(16), default="ERROR")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|acknowledged|resolved
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    ml_confidence: Mapped[float] = mapped_column(Float, default=0.0)  # best cosine similarity
    max_anomaly_score: Mapped[float] = mapped_column(Float, default=0.0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    example_message: Mapped[str] = mapped_column(Text, default="")
    # Background LLM worker — never blocks ingestion.
    severity: Mapped[str | None] = mapped_column(String(16))
    root_cause_component: Mapped[str | None] = mapped_column(String(64))
    triage: Mapped[dict | None] = mapped_column(JSON)
    triage_state: Mapped[str] = mapped_column(String(16), default="pending")  # pending|disabled|done|cached|error
    triage_attempts: Mapped[int] = mapped_column(Integer, default=0)
    triage_error: Mapped[str | None] = mapped_column(String(280))
    blast_radius: Mapped[dict | None] = mapped_column(JSON)             # {"services": [...], "source": svc}
    feedback: Mapped[dict | None] = mapped_column(JSON)                 # human verdict on the LLM triage
    resolution_notes: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class LLMCache(Base):
    """SHA-256(fingerprint) -> structured analysis. Dedup gate before any OpenRouter call."""

    __tablename__ = "llm_cache"

    fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(128))
    analysis: Mapped[dict] = mapped_column(JSON)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
