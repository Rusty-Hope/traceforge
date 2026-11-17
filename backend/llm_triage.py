"""Invisible background LLM worker — OpenRouter triage with SHA-256 dedup cache.

Constraints:
- Never on the hot path: ingestion + ML run locally at zero API cost.
- Only confirmed, deduplicated clusters are sent up: eligibility =
  (max anomaly score >= threshold) OR (recurring error cluster, >= min occurrences).
- One SQLite cache row per (prompt version, model, fingerprint): a repeated
  fingerprint is answered from cache, never re-billed. Token usage is recorded
  per call and surfaced on the dashboard.
- Output forced to JSON, validated against TriageReport, temperature 0.1.
- Triaged critical/high incidents fan out to the configured webhook (fire-and-forget).
"""
from __future__ import annotations

import hashlib
import json
import logging

import httpx
from openai import BadRequestError, OpenAI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .events import publish
from .models import Incident, LLMCache
from .schemas import TriageReport

log = logging.getLogger("traceforge.llm")

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = (
    "You are the triage engine inside a telemetry pipeline for a microservice fleet. "
    "Input is a compact statistical digest of ONE error cluster already confirmed "
    "anomalous by a local IsolationForest. Respond with ONLY a JSON object:\n"
    '{"severity": "critical|high|medium|low",\n'
    ' "root_cause_component": "concrete service/component name, max 40 chars",\n'
    ' "technical_explanation": "2-3 precise sentences grounded in the digest evidence",\n'
    ' "remediation_steps": ["3-6 imperative, specific steps"],\n'
    ' "suggested_patch_snippet": "short config/code diff addressing the root cause"}'
)


def cache_key(fingerprint: str) -> str:
    """SHA-256 dedup key — implicitly invalidates cache when model/prompt changes."""
    s = get_settings()
    return hashlib.sha256(f"{PROMPT_VERSION}|{s.llm_model}|{fingerprint}".encode()).hexdigest()


def llm_active() -> bool:
    s = get_settings()
    return s.llm_enabled and bool(s.openrouter_api_key)


def is_eligible(inc: Incident) -> bool:
    s = get_settings()
    return inc.max_anomaly_score >= s.anomaly_threshold or inc.occurrences >= s.triage_min_occurrences


_client: OpenAI | None = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        s = get_settings()
        _client = OpenAI(
            base_url=s.openrouter_base_url,
            api_key=s.openrouter_api_key,
            timeout=30.0,
            default_headers={"HTTP-Referer": "http://localhost:5173", "X-Title": "TraceForge"},
        )
    return _client


def run_triage_sweep(db: Session) -> dict:
    """One worker pass over the triage backlog. Cheap when idle; bounded when busy."""
    s = get_settings()
    backlog = db.scalars(
        select(Incident)
        .where(Incident.triage_state.in_(("pending", "error", "disabled")),
               Incident.triage_attempts < 3)
        .order_by(Incident.max_anomaly_score.desc(), Incident.last_seen.desc())
        .limit(100)
    ).all()

    disabled = 0
    if not llm_active():
        for inc in backlog:
            if inc.triage_state == "pending":
                inc.triage_state = "disabled"
                disabled += 1
        db.commit()
        return {"processed": 0, "cached": 0, "failed": 0, "disabled": disabled, "requeued": 0}

    requeued, todo = 0, []
    for inc in backlog:
        if inc.triage_state == "disabled":  # API key appeared -> re-arm
            inc.triage_state = "pending"
            requeued += 1
        elif is_eligible(inc):
            todo.append(inc)

    done = cached = failed = 0
    alerted: list[Incident] = []
    for inc in todo[: s.triage_batch]:
        state, report = _triage_one(db, inc)
        done += state == "done"
        cached += state == "cached"
        failed += state not in ("done", "cached")
        if report is not None:
            alerted.append(inc)
    db.commit()

    for inc in alerted:  # after commit: webhook consumers may read the incident via API
        _dispatch_alert(inc)
    for inc in todo[: s.triage_batch]:
        if inc.triage_state in ("done", "cached"):
            publish("incident_triaged", {"id": inc.id, "severity": inc.severity,
                                         "component": inc.root_cause_component,
                                         "state": inc.triage_state})
    return {"processed": done, "cached": cached, "failed": failed,
            "disabled": 0, "requeued": requeued}


# ── internals ────────────────────────────────────────────────────────────────
def _triage_one(db: Session, inc: Incident) -> tuple[str, "TriageReport | None"]:
    key = cache_key(inc.fingerprint)
    hit = db.get(LLMCache, key)
    if hit is not None:
        hit.hit_count += 1
        _apply(inc, hit.analysis, "cached")
        return "cached", None
    try:
        report, usage = _call_llm(_digest(inc))
    except Exception as exc:  # network / rate limit / schema — retried next sweep
        inc.triage_state = "error"
        inc.triage_attempts += 1
        inc.triage_error = f"{type(exc).__name__}: {exc}"[:280]
        log.warning("triage failed for incident #%s: %s", inc.id, inc.triage_error)
        return "error", None
    _apply(inc, report.model_dump(), "done")
    db.add(LLMCache(fingerprint=key, model=get_settings().llm_model, analysis=report.model_dump(),
                    prompt_tokens=usage["prompt"], completion_tokens=usage["completion"]))
    log.info("triaged incident #%s severity=%s component=%s (%d+%d tokens)",
             inc.id, report.severity, report.root_cause_component, usage["prompt"], usage["completion"])
    return "done", report


def _apply(inc: Incident, analysis: dict, state: str) -> None:
    inc.triage = analysis
    inc.severity = analysis.get("severity")
    inc.root_cause_component = analysis.get("root_cause_component")
    inc.triage_state = state
    inc.triage_error = None


SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _dispatch_alert(inc: Incident) -> None:
    """Fire-and-forget webhook for high/critical triaged incidents."""
    s = get_settings()
    severity = inc.severity or "low"
    if not s.alert_webhook_url or SEVERITY_ORDER.get(severity, 0) < SEVERITY_ORDER.get(s.alert_min_severity, 2):
        return
    payload = {
        "text": f"[{(inc.severity or '?').upper()}] {inc.service}: {inc.title[:120]}",
        "incident_id": inc.id,
        "severity": inc.severity,
        "root_cause_component": inc.root_cause_component,
        "technical_explanation": (inc.triage or {}).get("technical_explanation"),
        "remediation_steps": (inc.triage or {}).get("remediation_steps"),
        "dashboard_url": f"http://localhost:5173/?incident={inc.id}",
    }
    try:
        httpx.post(s.alert_webhook_url, json=payload, timeout=5.0)
        log.info("alert dispatched for incident #%s -> %s", inc.id, s.alert_webhook_url)
    except Exception as exc:
        log.warning("alert webhook failed for incident #%s: %s", inc.id, exc)


def _digest(inc: Incident) -> dict:
    """Compact evidence payload — never raw bulk telemetry."""
    return {
        "service": inc.service,
        "level": inc.level,
        "canonical_template": inc.title,
        "occurrences": inc.occurrences,
        "first_seen": inc.first_seen.isoformat(),
        "last_seen": inc.last_seen.isoformat(),
        "max_anomaly_score": round(inc.max_anomaly_score, 3),
        "cluster_confidence": round(inc.ml_confidence, 3),
        "example_log": inc.example_message[:400],
    }


def _call_llm(digest: dict) -> tuple[TriageReport, dict]:
    s = get_settings()
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(digest, separators=(",", ":"))},
    ]
    raw, usage = _completion(messages, s)
    try:
        return TriageReport.model_validate_json(_extract_json(raw)), usage
    except ValidationError:
        messages.append({"role": "assistant", "content": raw[:800]})
        messages.append({"role": "user", "content": "Your reply did not match the schema. Respond again with ONLY the corrected JSON object."})
        raw2, usage2 = _completion(messages, s)
        report = TriageReport.model_validate_json(_extract_json(raw2))
        usage = {k: usage[k] + usage2[k] for k in usage}
        return report, usage


def _completion(messages: list[dict], s: Settings) -> tuple[str, dict]:
    kwargs = dict(model=s.llm_model, messages=messages,
                  temperature=s.llm_temperature, max_tokens=s.llm_max_tokens)
    try:
        resp = _get_client().chat.completions.create(**kwargs, response_format={"type": "json_object"})
    except BadRequestError:  # model without JSON mode
        resp = _get_client().chat.completions.create(**kwargs)
    u = getattr(resp, "usage", None)
    usage = {"prompt": getattr(u, "prompt_tokens", 0) or 0,
             "completion": getattr(u, "completion_tokens", 0) or 0}
    return resp.choices[0].message.content or "", usage


def _extract_json(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
    if t.endswith("```"):
        t = t.rsplit("```", 1)[0]
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if 0 <= i < j else t.strip()
