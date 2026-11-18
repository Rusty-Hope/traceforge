# Security Policy

## Supported Versions

| Version | Supported |
|---|---|
| `0.4.x` (`main`) | Yes |
| `< 0.4.0` | No |

## Deployment Hardening Notes

* **External Ingestion (`POST /api/ingest`):** Set `INGEST_TOKEN` in `.env` so callers must supply a matching `X-Ingest-Token` header (compared in constant time via `hmac.compare_digest`).
* **Secrets:** Never commit `.env` or API keys (`OPENROUTER_API_KEY`). Use `.env.example` as a template only.
* **Network Exposure:** TraceForge is designed as a local-first / internal telemetry dashboard. If deploying on a shared network, place it behind an authenticated reverse proxy, as management and simulation endpoints (`POST /api/simulate/incident`, `/status`, `/notes`) are unauthenticated by default.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.
Instead, report the vulnerability privately using [GitHub Security Advisories](https://github.com/Rusty-Hope/traceforge/security/advisories/new).

Include:
* Affected component or endpoint
* Steps to reproduce or proof of concept
* Potential impact and suggested mitigation (if known)
