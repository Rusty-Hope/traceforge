"""Local OpenRouter-compatible mock — run the triage worker with zero API cost.

    make mock-llm
    OPENROUTER_BASE_URL=http://127.0.0.1:9938/v1 OPENROUTER_API_KEY=mock make api
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request

app = FastAPI(title="mock-openrouter")

_RESPONSE = {
    "severity": "high",
    "root_cause_component": "postgres-primary",
    "technical_explanation": (
        "Connection pool exhaustion on the primary database caused dependent services "
        "to fail their queries; the elevated error rate and latency digest indicate a "
        "saturated pool rather than an application defect."
    ),
    "remediation_steps": [
        "Raise pool_size/overflow or front Postgres with pgbouncer in transaction mode",
        "Set statement_timeout so long transactions cannot pin connections",
        "Alert when pool checkout wait time exceeds 1s",
    ],
    "suggested_patch_snippet": (
        "- engine = create_engine(url, pool_size=20)\n"
        "+ engine = create_engine(url, pool_size=40, pool_pre_ping=True,\n"
        "+                        connect_args={'options': '-c statement_timeout=5000'})"
    ),
}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    return {
        "id": "mock-cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model", "mock"),
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(_RESPONSE)},
        }],
        "usage": {"prompt_tokens": 180, "completion_tokens": 160, "total_tokens": 340},
    }


# Webhook receiver — point ALERT_WEBHOOK_URL here to watch alerts fire:
#   ALERT_WEBHOOK_URL=http://127.0.0.1:9938/alerts
_ALERTS: list[dict] = []


@app.post("/alerts")
async def receive_alert(request: Request):
    _ALERTS.append(await request.json())
    return {"ok": True}


@app.get("/alerts")
async def list_alerts():
    return _ALERTS
