"""Markdown post-mortem rendering: local-ML evidence + LLM triage + human context.

Never calls the LLM — everything is already in SQLite.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .llm_triage import cache_key
from .ml_engine import window_start
from .models import Incident, LLMCache, MetricWindow


def render_postmortem(db: Session, inc: Incident) -> str:
    triage = inc.triage or {}
    win_start = window_start(inc.first_seen, get_settings().window_size_s)
    worst = db.scalar(
        select(MetricWindow)
        .where(
            MetricWindow.service == inc.service,
            MetricWindow.is_anomaly.is_(True),
            MetricWindow.ts >= win_start,
            MetricWindow.ts <= inc.last_seen,
        )
        .order_by(MetricWindow.anomaly_score.desc())
        .limit(1)
    )
    if worst is None:
        worst = db.scalar(
            select(MetricWindow)
            .where(MetricWindow.service == inc.service, MetricWindow.is_anomaly.is_(True))
            .order_by(MetricWindow.anomaly_score.desc())
            .limit(1)
        )
    cache_row = db.get(LLMCache, cache_key(inc.fingerprint))
    model = cache_row.model if cache_row else "n/a"
    duration = max(0, int(((inc.resolved_at or inc.last_seen) - inc.first_seen).total_seconds()))

    lines = [
        f"# Post-Mortem: Incident #{inc.id} — {inc.service}",
        "",
        f"- **Canonical fingerprint:** `{inc.fingerprint[:16]}...`",
        f"- **Status:** {inc.status} | **Severity:** {inc.severity or 'unclassified'} | **Occurrences:** {inc.occurrences}",
        f"- **First seen:** {inc.first_seen.isoformat(sep=' ', timespec='seconds')}",
        f"- **Last seen:** {inc.last_seen.isoformat(sep=' ', timespec='seconds')}",
        f"- **Resolved at:** {inc.resolved_at.isoformat(sep=' ', timespec='seconds') if inc.resolved_at else '-'}",
        f"- **Active window:** ~{duration}s",
        "",
        "## Canonical template",
        "```text",
        inc.title,
        "```",
        "",
        "## Detection (local ML, zero API cost)",
        f"- Cluster confidence (TF-IDF cosine): **{inc.ml_confidence:.2f}**",
        f"- Max anomaly score (IsolationForest): **{inc.max_anomaly_score:.3f}**",
    ]
    if worst is not None:
        lines.append(
            f"- Worst flagged window on `{inc.service}`: error_rate={worst.error_rate:.2f}, "
            f"p95={worst.p95_latency_ms:.0f}ms, anomaly_score={worst.anomaly_score:.3f}"
        )

    radius = (inc.blast_radius or {}).get("services") or []
    if radius:
        lines += ["", "## Blast radius", ", ".join(f"`{svc}`" for svc in radius),
                  "", "_Services whose error-rate windows correlate with this incident._"]

    lines += ["", "## Root-cause analysis (background LLM triage)"]
    if triage:
        tokens = ""
        if cache_row and (cache_row.prompt_tokens or cache_row.completion_tokens):
            tokens = f" · {cache_row.prompt_tokens}+{cache_row.completion_tokens} tokens"
        lines += [
            f"- **Component:** {triage.get('root_cause_component', '-')}",
            f"- **Triaged by:** `{model}` ({inc.triage_state}{tokens})",
            "",
            triage.get("technical_explanation", "-"),
        ]
    else:
        lines.append("_Not yet triaged._")

    steps = triage.get("remediation_steps") or []
    if steps:
        lines += ["", "## Remediation"]
        lines += [f"{i}. {step}" for i, step in enumerate(steps, 1)]

    snippet = (triage.get("suggested_patch_snippet") or "").rstrip()
    if snippet:
        lines += ["", "## Suggested patch", "```diff", snippet, "```"]

    fb = inc.feedback or {}
    if fb:
        lines += ["", "## Human feedback"]
        lines.append(f"- Verdict: **{fb.get('verdict')}**")
        if fb.get("actual_cause"):
            lines.append(f"- Actual cause: {fb['actual_cause']}")

    if inc.resolution_notes:
        lines += ["", "## Resolution notes", inc.resolution_notes]

    lines.append("")
    return "\n".join(lines)
