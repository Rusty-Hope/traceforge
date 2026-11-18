"""Local OpenRouter-compatible mock — run the triage worker with zero API cost.

    make mock-llm
    OPENROUTER_BASE_URL=http://127.0.0.1:9938/v1 OPENROUTER_API_KEY=mock make api
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request

app = FastAPI(title="mock-openrouter")


def _select_triage(digest: dict) -> dict:
    svc = str(digest.get("service", "unknown-service"))
    tpl = str(digest.get("canonical_template", "")).lower()
    score = float(digest.get("max_anomaly_score", 0.0))

    if "pool exhausted" in tpl or "too many clients" in tpl:
        return {
            "severity": "critical" if score >= 0.85 else "high",
            "root_cause_component": "postgres-primary",
            "technical_explanation": (
                f"Connection pool exhaustion detected on {svc} due to saturated upstream Postgres "
                "connections. Elevated query wait times and 5xx errors indicate pool contention "
                "rather than an application logic regression."
            ),
            "remediation_steps": [
                "Raise pool_size/max_overflow or route traffic through PgBouncer in transaction mode",
                "Enforce statement_timeout so stalled queries cannot pin pool slots",
                "Alert when connection checkout wait exceeds 1000ms",
            ],
            "suggested_patch_snippet": (
                "- engine = create_engine(url, pool_size=20)\n"
                "+ engine = create_engine(url, pool_size=40, pool_pre_ping=True,\n"
                "+                        connect_args={'options': '-c statement_timeout=5000'})"
            ),
        }

    if "circuit breaker" in tpl or "circuit_open" in tpl:
        return {
            "severity": "critical",
            "root_cause_component": "postgres-primary",
            "technical_explanation": (
                f"Circuit breaker tripped on {svc} following consecutive upstream failures. "
                "Requests are now failing fast with 503 while downstream dependencies remain degraded."
            ),
            "remediation_steps": [
                "Inspect upstream health on postgres-primary and clear stuck transactions",
                "Keep half-open probe rate low so recovery traffic does not re-trip the breaker",
                "Enable degraded read fallback or queue non-critical writes",
            ],
            "suggested_patch_snippet": (
                "- breaker = CircuitBreaker(fail_max=8, reset_timeout=30)\n"
                "+ breaker = CircuitBreaker(fail_max=15, reset_timeout=15, half_open_calls=3)"
            ),
        }

    if "memoryerror" in tpl or "oom-killer" in tpl or "gc pause" in tpl:
        return {
            "severity": "critical" if ("oom" in tpl or "memoryerror" in tpl) else "high",
            "root_cause_component": svc[:40],
            "technical_explanation": (
                f"Heap pressure and prolonged GC pauses on {svc} escalated to worker termination. "
                "Allocation rate outpaced reclamation, pointing to unbounded in-memory buffering or cache growth."
            ),
            "remediation_steps": [
                f"Capture a heap dump from {svc} and inspect retained request/cache objects",
                "Cap worker concurrency and bound in-process caches with LRU eviction",
                "Restart degraded pods gracefully before kernel oom-killer triggers",
            ],
            "suggested_patch_snippet": (
                "- CACHE = {}\n"
                "+ from functools import lru_cache\n"
                "+ @lru_cache(maxsize=4096)"
            ),
        }

    if "latency slo" in tpl or "upstream_timeout" in tpl or "timeouterror" in tpl:
        return {
            "severity": "high",
            "root_cause_component": svc[:40],
            "technical_explanation": (
                f"Tail latency (p95) breached the 300ms SLO on {svc}, triggering upstream request timeouts. "
                "Retry amplification can worsen tail latency if deadlines are not propagated."
            ),
            "remediation_steps": [
                f"Inspect slow spans and downstream saturation on {svc}",
                "Enforce per-hop request deadlines shorter than the gateway timeout",
                "Add exponential backoff with jitter on upstream retries",
            ],
            "suggested_patch_snippet": (
                "- client = httpx.AsyncClient(timeout=30.0)\n"
                "+ client = httpx.AsyncClient(timeout=httpx.Timeout(2.5, connect=0.5))"
            ),
        }

    if "deadlock" in tpl:
        return {
            "severity": "high",
            "root_cause_component": "postgres-primary",
            "technical_explanation": (
                "Concurrent transactions acquired conflicting ShareLock rows in opposite order on "
                "postgres-primary, forcing Postgres to abort a victim transaction."
            ),
            "remediation_steps": [
                "Order multi-row updates consistently by primary key inside transactions",
                "Keep transactions short and avoid external RPCs while holding row locks",
                "Retry deadlock SQLSTATE 40P01 idempotently at the repository layer",
            ],
            "suggested_patch_snippet": (
                "- SELECT * FROM order_items WHERE order_id = :oid FOR UPDATE;\n"
                "+ SELECT * FROM order_items WHERE order_id = :oid ORDER BY id FOR UPDATE;"
            ),
        }

    if "keyring" in tpl:
        return {
            "severity": "medium",
            "root_cause_component": "auth-service",
            "technical_explanation": (
                "Primary keyring backend became unreachable on auth-service, causing fallback to the "
                "environment key provider. Token signing continues, but rotation guarantees are degraded."
            ),
            "remediation_steps": [
                "Check connectivity and permissions to the secret/keyring daemon",
                "Verify cached signing keys remain valid until keyring health recovers",
            ],
            "suggested_patch_snippet": "",
        }

    if "hard decline" in tpl or "payment declined" in tpl:
        return {
            "severity": "medium",
            "root_cause_component": svc[:40],
            "technical_explanation": (
                f"Elevated payment decline responses observed on {svc}. While localized to card/issuer "
                "rejections, sustained spikes warrant checking gateway routing and fraud rules."
            ),
            "remediation_steps": [
                "Correlate decline codes by processor/issuer region",
                "Confirm 4xx business declines are separated from 5xx transport errors in SLO alerts",
            ],
            "suggested_patch_snippet": "",
        }

    return {
        "severity": "high" if score >= 0.62 else "medium",
        "root_cause_component": svc[:40],
        "technical_explanation": (
            f"Recurring anomalous error cluster on {svc} (score={score:.2f}) matching template "
            f"'{digest.get('canonical_template', '')[:100]}'."
        ),
        "remediation_steps": [
            f"Inspect recent deployments and error traces on {svc}",
            "Verify downstream health checks and resource saturation metrics",
        ],
        "suggested_patch_snippet": "",
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    digest: dict = {}
    for msg in reversed(body.get("messages", [])):
        if msg.get("role") == "user":
            try:
                digest = json.loads(msg.get("content", "{}"))
                break
            except json.JSONDecodeError:
                pass
    triage = _select_triage(digest)
    return {
        "id": "mock-cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": body.get("model", "mock"),
        "choices": [{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": json.dumps(triage)},
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
