# Changelog

All notable changes to **TraceForge** are documented in this file.

## [0.4.0] - 2026-11-18

### Added
* Single-container multi-stage `Dockerfile` and automated GitHub Container Registry (`ghcr.io/rusty-hope/traceforge`) publishing workflow.
* `LogClusterer.warm_start` and `warm_engines` startup hydration from SQLite (`Incident` + `LogEvent`) so restarts preserve cluster state.
* Persistent cluster merge synchronization (`LogClusterer.drain_merges` → `_apply_cluster_merges`) consolidating merged `Incident` and `LogEvent` rows in SQLite.
* Incident search/text filter in `IncidentFeed`, raw sample `status_code`/`latency_ms` badges, and always-available Markdown post-mortem export.
* Scenario-aware triage responses in `backend/mock_llm.py`.
* FastAPI HTTP integration tests (`tests/test_api.py`), bringing the test suite to 35 tests.
* GitHub community profile files (`CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`, issue/PR templates, Dependabot, `pyproject.toml`).

### Fixed
* Two-phase `MetricWindow` flush before `_blast_radius` correlation inside `_analyze_cycle`, fixing missed cross-service correlations within the same batch.
* Consistent event-time propagation for `Incident.first_seen`/`last_seen`, timezone-aware timestamp normalization (`naive_utc`), and relative timeline anchoring in UI/API queries.
* `render_postmortem` now scopes the worst anomalous window lookup to the incident's active time range.
* `_run_burst` now aggregates `new_incidents` across all ticks of a fast-forward chaos injection.
* Constant-time `hmac.compare_digest` check for `X-Ingest-Token` and validation of `InjectRequest.service`.
* Immediate re-arming and processing of `disabled` incidents in `run_triage_sweep` once LLM becomes active.
