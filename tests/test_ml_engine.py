"""Unit tests for the local ML layer — normalization, clustering, detection, aggregation."""
from datetime import datetime, timedelta

import numpy as np
import pytest

from backend.ml_engine import (
    AnomalyDetector,
    LogClusterer,
    WindowAggregator,
    extract_window_features,
    feature_vector,
    fingerprint,
    normalize_message,
    pearson,
    shannon_entropy,
    window_start,
)


# ── normalization & fingerprints ─────────────────────────────────────────────
def test_normalize_message_is_stable_and_masks_dynamics():
    a = normalize_message('charge authorized id=ch_9f8e7d6c5b4a amount=$84.20 in 41ms')
    b = normalize_message('charge authorized id=ch_00ff11aa22bb amount=$12.99 in 250ms')
    assert a == b, "different ids/amounts must collapse to the same template"
    assert "<id>" in a and "<num>" in a
    assert "9f8e7d6c5b4a" not in a
    assert fingerprint(a) == fingerprint(b)
    assert len(fingerprint(a)) == 64


def test_normalize_message_preserves_component_names():
    out = normalize_message("GET /v1/orders 200 41ms upstream=orders-service")
    assert "orders-service" in out, "service names must survive normalization"
    assert "<path>" in out


def test_window_start_floors_to_grid():
    ts = datetime(2026, 1, 1, 10, 0, 7)
    assert window_start(ts, 5) == datetime(2026, 1, 1, 10, 0, 5)
    assert window_start(ts.replace(tzinfo=None), 60) == datetime(2026, 1, 1, 10, 0, 0)


def test_shannon_entropy_bounds():
    assert shannon_entropy(["same same same"]) < shannon_entropy(["a b c d e f", "x y z w q r"])


def test_pearson():
    assert pearson([1, 2, 3, 4], [2, 4, 6, 8]) == pytest.approx(1.0)
    assert pearson([1, 2, 3, 4], [8, 6, 4, 2]) == pytest.approx(-1.0)
    assert pearson([1, 1, 1], [1, 2, 3]) == 0.0  # zero variance
    assert pearson([1], [1]) == 0.0              # too short


# ── window features: batch vs incremental equivalence ────────────────────────
class _Ev:
    def __init__(self, ts, service, level="INFO", status_code=200, latency_ms=40.0,
                 message="GET /v1/orders 200 40ms", attributes=None):
        self.ts, self.service, self.level = ts, service, level
        self.status_code, self.latency_ms, self.message = status_code, latency_ms, message
        self.attributes = attributes or {}


def test_aggregator_matches_batch_extraction():
    base = datetime(2026, 1, 1)
    events = []
    for i in range(10):
        events.append(_Ev(base + timedelta(seconds=i), "orders-service",
                          status_code=500 if i == 3 else 200,
                          latency_ms=100.0 + i,
                          message=f"order created id=ord_{i:012d}"))
    events.append(_Ev(base + timedelta(seconds=1), "orders-service",
                      message="MemoryError: unable to allocate", level="ERROR", status_code=None,
                      attributes={"scenario": "memory_leak"}))

    agg = WindowAggregator(window_s=5)
    for e in events:
        agg.add(e)
    incremental = {(wf.service, wf.ts): wf for wf in
                   [agg.snapshot("orders-service", window_start(e.ts, 5)) for e in events]}
    batch = {(wf.service, wf.ts): wf for wf in extract_window_features(events, 5)}

    for key in batch:
        a, b = incremental[key], batch[key]
        assert a.error_rate == pytest.approx(b.error_rate)
        assert a.p95_latency_ms == pytest.approx(b.p95_latency_ms)
        assert a.log_entropy == pytest.approx(b.log_entropy, rel=1e-6)
        assert a.frac_5xx == pytest.approx(b.frac_5xx)
        assert a.contaminated == b.contaminated
    agg.evict_older_than(base + timedelta(seconds=100))
    assert len(agg) == 0


# ── anomaly detection ────────────────────────────────────────────────────────
def _feature_row(rng, anomaly=False):
    if anomaly:
        return np.array([0.55, 900.0, 700.0, 1.1, 10.0, 0.40, 0.05, 0.55])
    base = np.array([0.02, 40.0, 30.0, 3.2, 10.0, 0.97, 0.02, 0.01])
    return base + rng.normal(0, 0.02, 8)


def test_detector_flags_extreme_window():
    det = AnomalyDetector(contamination=0.05, min_train=20, retrain_every=10_000)
    rng = np.random.default_rng(0)
    for _ in range(40):
        det.observe(_feature_row(rng), contaminated=False)
    assert det.fitted
    normal = det.observe(_feature_row(rng), contaminated=True)      # not fed to baseline
    crazy = det.observe(_feature_row(rng, anomaly=True), contaminated=True)
    assert crazy > 0.62
    assert crazy > normal


def test_detector_effective_threshold_never_below_static():
    det = AnomalyDetector(contamination=0.05, min_train=10, retrain_every=10_000)
    rng = np.random.default_rng(1)
    for _ in range(30):
        det.observe(_feature_row(rng), contaminated=False, static_threshold=0.62, p95_ms=40.0)
    assert det.effective_threshold(0.62) >= 0.62
    assert det.p95_baseline() == pytest.approx(40.0, rel=0.5)


def test_detector_contaminated_windows_do_not_poison_baseline():
    det = AnomalyDetector(contamination=0.05, min_train=10, retrain_every=10_000)
    rng = np.random.default_rng(2)
    for _ in range(30):
        det.observe(_feature_row(rng), contaminated=False, static_threshold=0.62)
    size_before = det.baseline_size
    for _ in range(20):
        det.observe(_feature_row(rng, anomaly=True), contaminated=True, static_threshold=0.62)
    assert det.baseline_size == size_before


# ── log clustering ───────────────────────────────────────────────────────────
def test_clusterer_dedup_and_leader_merge():
    c = LogClusterer(similarity=0.5, min_boot=3, refit_every=10_000)
    for m in ("alpha beta gamma delta error one",
              "alpha beta gamma delta error two",
              "completely different text here now"):
        c.assign(m)
    assert c.warmed

    r1 = c.assign("alpha beta gamma delta error one")
    r2 = c.assign("alpha beta gamma delta error one")
    assert r1.created and not r2.created
    assert r1.fingerprint == r2.fingerprint
    assert r2.confidence == 1.0  # exact template hit

    # near-duplicate joins the leader cluster instead of forking
    r3 = c.assign("alpha beta gamma delta error two")
    assert not r3.created
    assert r3.fingerprint == r1.fingerprint
    assert r3.confidence >= 0.5


def test_clusterer_cold_start_buffers_until_min_boot():
    c = LogClusterer(similarity=0.6, min_boot=3)
    r = c.assign("alpha beta gamma delta error one")
    assert not c.warmed and r.created is False and r.confidence == 0.0
    c.assign("alpha beta gamma delta error two")
    c.assign("completely different text here now")
    assert c.warmed


def test_clusterer_warms_on_volume_even_without_cardinality():
    """Low-cardinality streams (few unique templates) must not stall the cold start."""
    c = LogClusterer(similarity=0.6, min_boot=2)
    for _ in range(19):
        c.assign("identical message over and over id=aaaa1111bbbb")
    assert not c.warmed  # fallback threshold is min_boot * 10 = 20 seen
    c.assign("identical message over and over id=aaaa1111bbbb")  # 20th seen -> warm
    assert c.warmed


def test_clusterer_merges_near_duplicate_clusters_on_refit():
    c = LogClusterer(similarity=0.99, min_boot=2, refit_every=10_000)
    c.assign("database pool exhausted timeout 30000 ms service one alpha beta gamma delta")
    c.assign("totally unrelated kernel panic at ring zero")  # warms the vectorizer
    r1 = c.assign("database pool exhausted timeout 30000 ms service one alpha beta gamma delta")
    r2 = c.assign("database pool exhausted timeout 30000 ms service two alpha beta gamma delta")
    assert r1.created and r2.created, "with a 0.99 threshold the pair must fork into two clusters"
    assert c.cluster_count == 3
    c._refit()  # refit merges near-duplicate centroids (cosine >= 0.85)
    assert c.cluster_count == 2


def test_fingerprint_keys_llm_cache():
    from backend.llm_triage import cache_key
    fp = fingerprint("some template")
    assert cache_key(fp) == cache_key(fp)
    assert len(cache_key(fp)) == 64
