import os
import tempfile

# Must be set before any backend import (get_settings is cached).
_TMP = tempfile.mkdtemp(prefix="traceforge-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/test.db"
os.environ["WINDOW_SIZE_S"] = "2"
os.environ["BASELINE_MIN_WINDOWS"] = "8"
os.environ["RETRAIN_EVERY"] = "1000"
os.environ["ANOMALY_THRESHOLD"] = "0.62"
os.environ["LLM_ENABLED"] = "false"
os.environ["SIMULATE_ENABLED"] = "false"

import pytest  # noqa: E402


@pytest.fixture()
def db():
    from backend import pipeline
    from backend.database import Base, get_engine, get_session, init_db

    init_db()
    Base.metadata.drop_all(get_engine())
    Base.metadata.create_all(get_engine())
    pipeline.reset_engines()
    pipeline.clusterer().min_boot = 1  # deterministic warm start in tests
    session = get_session()
    yield session
    session.close()
