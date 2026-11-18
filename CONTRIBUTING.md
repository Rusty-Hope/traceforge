# Contributing to TraceForge

Thanks for your interest in contributing to **TraceForge**!

## Design Principles

Please keep changes aligned with the project's core principles:

1. **Zero chatbot UI** — the LLM stays strictly in the background worker, writing validated JSON into SQLite.
2. **No generic AI aesthetics** — high-density, monochrome, industrial zinc UI (`1px` borders, monospace telemetry).
3. **Local ML first, LLM second** — hot-path ingestion, windowing, anomaly detection, and clustering must remain local and free.

## Development Setup

```bash
git clone https://github.com/Rusty-Hope/traceforge.git
cd traceforge
cp .env.example .env
make install
```

Run backend + frontend locally:

```bash
# Terminal 1 — FastAPI + simulator + triage worker
make api

# Terminal 2 — Vite dev server (proxies /api to :8000)
make web-dev
```

Optional zero-token LLM worker testing:

```bash
make mock-llm
OPENROUTER_BASE_URL=http://127.0.0.1:9938/v1 OPENROUTER_API_KEY=mock make api
```

## Verification Checklist

Before opening a pull request, run:

```bash
# Backend test suite
make test

# Frontend type-check & production build
cd frontend && npm run build
```

## Pull Request Process

1. Fork the repository and create a branch from `main`.
2. Add or update unit/integration tests in `tests/` for any behavioral change.
3. Ensure both `pytest` and `npm run build` pass cleanly.
4. Fill out the pull request template clearly describing what changed and how it was verified.
