"""Central settings — every tunable lives here and maps 1:1 to .env."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Storage
    database_url: str = "sqlite:///./traceforge.db"

    # Local ML layer
    window_size_s: int = 15
    baseline_min_windows: int = 30
    retrain_every: int = 50
    anomaly_threshold: float = 0.62
    contamination: float = 0.02
    cluster_similarity: float = 0.60

    # Background LLM worker (OpenRouter)
    llm_enabled: bool = True
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "z-ai/glm-5.3-flash"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 450

    # Background triage worker
    triage_interval_s: float = 4.0
    triage_min_occurrences: int = 3
    triage_batch: int = 2

    # Simulator
    simulate_enabled: bool = True
    simulate_interval_s: float = 0.5   # wall-clock sleep between ticks
    sim_step_s: float = 5.0            # simulated seconds advanced per tick

    # Data hygiene & integrations
    retention_hours: int = 24          # raw events older than this are pruned (windows/incidents kept)
    ingest_token: str = ""             # if set, POST /api/ingest requires X-Ingest-Token
    alert_webhook_url: str = ""        # POSTed on triaged critical/high incidents; empty = off
    alert_min_severity: str = "high"

    # CORS (frontend dev server)
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
