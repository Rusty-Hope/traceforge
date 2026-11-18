"""Tests for the LLM worker contracts, dedup cache keys, and post-mortem rendering."""
from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from sqlalchemy import select

from backend.llm_triage import _digest, _extract_json, cache_key, run_triage_sweep
from backend.models import Incident, LLMCache, MetricWindow
from backend.reporting import render_postmortem
from backend.schemas import TriageReport

VALID = {
    "severity": "high",
    "root_cause_component": "postgres-primary",
    "technical_explanation": "Pool exhausted; dependent services timed out on checkout.",
    "remediation_steps": ["Raise pool_size", "Set statement_timeout", "Add pool wait alerts"],
    "suggested_patch_snippet": "- pool_size=20\n+ pool_size=40",
}


def test_triage_report_accepts_valid_and_rejects_invalid():
    assert TriageReport.model_validate(VALID).severity == "high"
    with pytest.raises(ValidationError):
        TriageReport.model_validate({**VALID, "severity": "apocalyptic"})
    with pytest.raises(ValidationError):
        TriageReport.model_validate({**VALID, "remediation_steps": []})


def test_cache_key_depends_on_model_and_prompt_version():
    fp = "a" * 64
    k1 = cache_key(fp)
    assert k1 == cache_key(fp)
    assert len(k1) == 64
    import backend.llm_triage as m
    m.PROMPT_VERSION = "v2"
    try:
        assert cache_key(fp) != k1, "rotating the prompt must invalidate the cache"
    finally:
        m.PROMPT_VERSION = "v1"


def test_extract_json_strips_fences_and_prose():
    assert _extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _extract_json('Sure! {"a": 1} hope that helps') == '{"a": 1}'
    assert _extract_json('{"a": {"b": 2}}') == '{"a": {"b": 2}}'


def test_sweep_without_key_marks_pending_disabled(db):
    db.add(Incident(fingerprint="f" * 64, service="orders-service", level="ERROR",
                    title="boom id=<id>", occurrences=10, max_anomaly_score=0.9))
    db.commit()
    summary = run_triage_sweep(db)
    assert summary["disabled"] == 1 and summary["processed"] == 0
    inc = db.scalar(select(Incident))
    assert inc.triage_state == "disabled"


def test_sweep_with_cache_hit_never_calls_llm(db, monkeypatch):
    fp = "f" * 64
    db.add(LLMCache(fingerprint=cache_key(fp), model="mock", analysis=VALID,
                    prompt_tokens=100, completion_tokens=50))
    db.add(Incident(fingerprint=fp, service="orders-service", level="ERROR",
                    title="boom id=<id>", occurrences=10, max_anomaly_score=0.9))
    db.commit()

    import backend.llm_triage as m
    monkeypatch.setattr(m, "llm_active", lambda: True)

    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("LLM must not be called on a cache hit")

    monkeypatch.setattr(m, "_completion", _boom)
    summary = run_triage_sweep(db)
    assert summary["cached"] == 1 and called["n"] == 0
    inc = db.scalar(select(Incident))
    assert inc.triage_state == "cached"
    assert inc.severity == "high"
    assert db.scalar(select(LLMCache)).hit_count == 1


def test_postmortem_contains_all_sections(db):
    inc = Incident(fingerprint="e" * 64, service="orders-service", level="ERROR",
                   title="pool exhausted id=<id>", occurrences=42,
                   ml_confidence=0.95, max_anomaly_score=0.83,
                   severity="critical", triage_state="done", triage=VALID,
                   blast_radius={"services": ["payments-service"], "source": "orders-service"},
                   feedback={"verdict": "confirmed", "actual_cause": None},
                   resolution_notes="Bounced the pool; raised pool_size.")
    db.add(inc)
    db.add(LLMCache(fingerprint=cache_key("e" * 64), model="z-ai/glm-5.3-flash",
                    analysis=VALID, prompt_tokens=180, completion_tokens=160))
    db.commit()
    inc = db.scalar(select(Incident))
    md = render_postmortem(db, inc)
    for section in ("Post-Mortem", "Canonical template", "Detection", "Blast radius",
                    "Root-cause analysis", "Remediation", "Suggested patch",
                    "Human feedback", "Resolution notes"):
        assert section in md, f"missing section: {section}"
    assert "z-ai/glm-5.3-flash" in md and "180+160 tokens" in md  # model provenance + token usage
    assert "payments-service" in md


def test_sweep_rearms_and_processes_disabled_incidents_in_same_pass(db, monkeypatch):
    inc = Incident(
        fingerprint="d" * 64, service="orders-service", level="ERROR",
        title="boom id=<id>", occurrences=5, max_anomaly_score=0.9,
        triage_state="disabled", blast_radius={"services": ["api-gateway"], "source": "orders-service"},
    )
    db.add(inc)
    db.commit()

    digest = _digest(inc)
    assert digest["blast_radius_services"] == ["api-gateway"]

    import backend.llm_triage as m
    monkeypatch.setattr(m, "llm_active", lambda: True)
    monkeypatch.setattr(m, "_completion", lambda msgs, s: (
        '{"severity":"high","root_cause_component":"orders-service",'
        '"technical_explanation":"Timed out","remediation_steps":["Restart"],'
        '"suggested_patch_snippet":""}',
        {"prompt": 50, "completion": 30},
    ))
    summary = run_triage_sweep(db)
    assert summary["requeued"] == 1
    assert summary["processed"] == 1
    refreshed = db.scalar(select(Incident))
    assert refreshed.triage_state == "done"


def test_postmortem_prefers_worst_window_within_incident_range(db):
    t0 = datetime(2026, 1, 1, 10, 0, 0)
    db.add(MetricWindow(ts=t0, service="orders-service", is_anomaly=True,
                        error_rate=0.4, p95_latency_ms=450.0, anomaly_score=0.75))
    db.add(MetricWindow(ts=t0 + timedelta(hours=2), service="orders-service", is_anomaly=True,
                        error_rate=0.9, p95_latency_ms=2500.0, anomaly_score=0.99))
    db.add(Incident(
        fingerprint="c" * 64, service="orders-service", level="ERROR",
        title="scoped error", occurrences=4, ml_confidence=0.9, max_anomaly_score=0.75,
        first_seen=t0, last_seen=t0 + timedelta(seconds=10),
    ))
    db.commit()
    inc = db.scalar(select(Incident))
    md = render_postmortem(db, inc)
    assert "p95=450ms" in md
    assert "p95=2500ms" not in md
