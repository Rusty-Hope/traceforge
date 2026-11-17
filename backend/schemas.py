"""API + LLM contracts (Pydantic v2)."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Severity = Literal["critical", "high", "medium", "low"]


class TriageReport(BaseModel):
    """Strict output contract for the background LLM worker."""

    severity: Severity
    root_cause_component: str = Field(min_length=1, max_length=64)
    technical_explanation: str = Field(min_length=1, max_length=1200)
    remediation_steps: list[str] = Field(min_length=1, max_length=8)
    suggested_patch_snippet: str = Field(default="", max_length=1200)


# ── API responses ────────────────────────────────────────────────────────────
class WindowPoint(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    service: str
    p95_latency_ms: float
    error_rate: float
    rps: float
    log_entropy: float
    anomaly_score: float
    is_anomaly: bool
    contaminated: bool
    forecast_risk: float = 0.0


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    fingerprint: str
    service: str
    level: str
    status: str
    title: str
    occurrences: int
    ml_confidence: float
    max_anomaly_score: float
    first_seen: datetime
    last_seen: datetime
    severity: str | None = None
    root_cause_component: str | None = None
    triage_state: str = "pending"
    triage_attempts: int = 0
    triage_error: str | None = None
    triage: dict | None = None
    blast_radius: dict | None = None
    feedback: dict | None = None
    resolution_notes: str | None = None
    resolved_at: datetime | None = None


class EventSample(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    ts: datetime
    service: str
    level: str
    message: str
    status_code: int | None = None
    latency_ms: float | None = None
    trace_id: str


class IncidentDetail(IncidentOut):
    samples: list[EventSample] = []
    tokens: dict | None = None  # {"prompt": int, "completion": int} from the dedup cache


class StatusUpdate(BaseModel):
    status: Literal["open", "acknowledged", "resolved"]


class FeedbackIn(BaseModel):
    verdict: Literal["confirmed", "rejected"]
    actual_cause: str | None = Field(default=None, max_length=300)


class NotesIn(BaseModel):
    notes: str = Field(default="", max_length=4000)


class InjectRequest(BaseModel):
    kind: Literal["latency_spike", "memory_leak", "db_pool_exhaustion", "cascade_failure"] = "db_pool_exhaustion"
    service: str | None = None
    ticks: int = Field(20, ge=2, le=120)
    intensity: float = Field(1.0, ge=0.1, le=5.0)
    fast_forward: bool = False


class InjectResponse(BaseModel):
    scenario: dict
    fast_forwarded: bool
    events_generated: int = 0
    new_incidents: list[dict] = []
    note: str = ""


class IngestEvent(BaseModel):
    """External telemetry batch item — e.g. from Vector / Fluent Bit / your app."""
    service: str = Field(min_length=1, max_length=64)
    level: str = "INFO"
    message: str = Field(min_length=1, max_length=4000)
    status_code: int | None = None
    latency_ms: float | None = None
    ts: datetime | None = None
    trace_id: str | None = None
    attributes: dict = {}


class IngestResponse(BaseModel):
    accepted: int
    ingested: int
    analyzed: int


# ── engine introspection ─────────────────────────────────────────────────────
class DetectorInfo(BaseModel):
    fitted: bool
    baseline_size: int
    threshold: float


class ClustererInfo(BaseModel):
    warmed: bool
    clusters: int
    corpus: int


class LLMInfo(BaseModel):
    enabled: bool
    model: str
    cache_entries: int
    cache_hits: int
    tokens_prompt: int = 0
    tokens_completion: int = 0


class SystemStats(BaseModel):
    events_total: int
    incidents_open: int
    anomalies_1h: int


class EngineStatus(BaseModel):
    clusterer: ClustererInfo
    detectors: dict[str, DetectorInfo]
    llm: LLMInfo
    stats: SystemStats


class ServiceSummary(BaseModel):
    service: str
    detector_fitted: bool
    baseline_size: int
    open_incidents: int
    latest: WindowPoint | None = None


class HealthOut(BaseModel):
    status: str
    simulate_enabled: bool
    llm_active: bool
    llm_model: str
