"""Local classic-ML layer — zero network calls, zero API cost.

- extract_window_features : batch feature extraction (used by tests / one-off analysis).
- WindowAggregator        : incremental per-(service, window) accumulators — no full rebuilds.
- AnomalyDetector         : per-service IsolationForest, z-normalized scores in [0, 1],
                            adaptive threshold (quantile of clean score history),
                            p95 baseline for drift forecasting.
- LogClusterer            : TF-IDF + cosine leader clustering into canonical incident
                            fingerprints (SHA-256), with automatic merge of near-duplicate
                            clusters on refit.

Engines are intentionally not thread-safe; one process owns them and
pipeline.analyze_cycle() serializes access.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple

import numpy as np
from scipy.sparse import csr_matrix
from sklearn.ensemble import IsolationForest
from sklearn.feature_extraction.text import TfidfVectorizer

FEATURE_NAMES = (
    "error_rate", "p95_latency_ms", "mean_latency_ms", "log_entropy",
    "rps", "frac_2xx", "frac_4xx", "frac_5xx",
)

# ── message normalization ────────────────────────────────────────────────────
_NORMALIZERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<uuid>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?"), "<ip>"),
    (re.compile(r"\b[0-9a-f]{10,}\b", re.I), "<hex>"),
    (re.compile(r"0x[0-9a-f]+", re.I), "<hex>"),
    (re.compile(r'"[^"]*"|\'[^\']*\''), "<str>"),
    (re.compile(r"(?<![\w$])/[\w.@:/-]+"), "<path>"),
    (re.compile(r"\b[a-z]{2,4}[_-][0-9a-z]{6,}\b"), "<id>"),
    (re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|kb|mb|gb|%)?\b", re.I), "<num>"),
    (re.compile(r"\s+"), " "),
)


def normalize_message(message: str) -> str:
    """Collapse dynamic values into placeholders -> stable log template."""
    out = message.strip()
    for pattern, repl in _NORMALIZERS:
        out = pattern.sub(repl, out)
    return out


def fingerprint(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()


def shannon_entropy(texts: Iterable[str]) -> float:
    tokens = [tok for text in texts for tok in text.split()]
    return _entropy(Counter(tokens), len(tokens))


def shannon_entropy_from_counts(counts: Counter, total: int) -> float:
    return _entropy(counts, total)


def _entropy(counts: Counter, n: int) -> float:
    if n <= 0:
        return 0.0
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def pearson(a: Iterable[float], b: Iterable[float]) -> float:
    """Pearson correlation; 0.0 when undefined (too short / zero variance)."""
    A, B = np.asarray(list(a), dtype=np.float64), np.asarray(list(b), dtype=np.float64)
    if len(A) != len(B) or len(A) < 3:
        return 0.0
    sa, sb = float(A.std()), float(B.std())
    if sa < 1e-9 or sb < 1e-9:
        return 0.0
    return float(((A - A.mean()) * (B - B.mean())).mean() / (sa * sb))


def _naive_utc(ts: datetime) -> datetime:
    return ts.astimezone(timezone.utc).replace(tzinfo=None) if ts.tzinfo else ts


def window_start(ts: datetime, window_s: int) -> datetime:
    ts = _naive_utc(ts)
    epoch = datetime(1970, 1, 1)
    secs = int((ts - epoch).total_seconds())
    return epoch + timedelta(seconds=secs // window_s * window_s)


# ── window features ──────────────────────────────────────────────────────────
@dataclass(slots=True)
class WindowFeatures:
    ts: datetime
    service: str
    error_rate: float
    p95_latency_ms: float
    mean_latency_ms: float
    log_entropy: float
    rps: float
    frac_2xx: float
    frac_4xx: float
    frac_5xx: float
    contaminated: bool = False


def feature_vector(obj: Any) -> np.ndarray:
    """Read FEATURE_NAMES attributes off any WindowFeatures / MetricWindow-like object."""
    return np.array([float(getattr(obj, name)) for name in FEATURE_NAMES], dtype=np.float64)


def extract_window_features(events: Iterable[Any], window_s: int) -> list[WindowFeatures]:
    """Batch aggregate raw log events into per-(service, window) feature rows."""
    buckets: dict[tuple[str, datetime], list[Any]] = {}
    for e in events:
        buckets.setdefault((e.service, window_start(e.ts, window_s)), []).append(e)

    out: list[WindowFeatures] = []
    for (service, ts), evts in sorted(buckets.items()):
        n = max(len(evts), 1)
        codes = [e.status_code if e.status_code is not None else 200 for e in evts]
        lat = [e.latency_ms for e in evts if e.latency_ms is not None]
        errors = sum(1 for e in evts if (e.status_code or 0) >= 500 or (e.level or "").upper() in {"ERROR", "FATAL"})
        out.append(WindowFeatures(
            ts=ts, service=service,
            error_rate=errors / n,
            p95_latency_ms=float(np.percentile(lat, 95)) if lat else 0.0,
            mean_latency_ms=float(np.mean(lat)) if lat else 0.0,
            log_entropy=shannon_entropy(normalize_message(e.message) for e in evts),
            rps=len(evts) / window_s,
            frac_2xx=sum(c < 400 for c in codes) / n,
            frac_4xx=sum(400 <= c < 500 for c in codes) / n,
            frac_5xx=sum(c >= 500 for c in codes) / n,
            contaminated=any((getattr(e, "attributes", None) or {}).get("scenario") for e in evts),
        ))
    return out


# ── incremental window aggregation ───────────────────────────────────────────
@dataclass(slots=True)
class _Bucket:
    count: int = 0
    errors: int = 0
    codes: Counter = field(default_factory=Counter)
    lat: list[float] = field(default_factory=list)
    token_counts: Counter = field(default_factory=Counter)
    contaminated: bool = False


class WindowAggregator:
    """Incremental per-(service, window) accumulators.

    O(1) work per event; snapshots (including p95) derived on demand.
    Windows are evicted two window-lengths after they close.
    """

    def __init__(self, window_s: int, max_lat_per_bucket: int = 4000) -> None:
        self.window_s = window_s
        self.max_lat = max_lat_per_bucket
        self._buckets: dict[tuple[str, datetime], _Bucket] = {}

    def add(self, event: Any) -> tuple[str, datetime]:
        start = window_start(event.ts, self.window_s)
        b = self._buckets.setdefault((event.service, start), _Bucket())
        b.count += 1
        code = event.status_code if event.status_code is not None else 200
        b.codes["2xx" if code < 400 else "4xx" if code < 500 else "5xx"] += 1
        if code >= 500 or (event.level or "").upper() in {"ERROR", "FATAL"}:
            b.errors += 1
        if event.latency_ms is not None and len(b.lat) < self.max_lat:
            b.lat.append(event.latency_ms)
        for tok in normalize_message(event.message).split():
            b.token_counts[tok] += 1
        if (getattr(event, "attributes", None) or {}).get("scenario"):
            b.contaminated = True
        return (event.service, start)

    def snapshot(self, service: str, start: datetime) -> WindowFeatures:
        b = self._buckets[(service, start)]
        n = max(b.count, 1)
        return WindowFeatures(
            ts=start, service=service,
            error_rate=b.errors / n,
            p95_latency_ms=float(np.percentile(b.lat, 95)) if b.lat else 0.0,
            mean_latency_ms=float(np.mean(b.lat)) if b.lat else 0.0,
            log_entropy=shannon_entropy_from_counts(b.token_counts, sum(b.token_counts.values())),
            rps=b.count / self.window_s,
            frac_2xx=b.codes["2xx"] / n,
            frac_4xx=b.codes["4xx"] / n,
            frac_5xx=b.codes["5xx"] / n,
            contaminated=b.contaminated,
        )

    def evict_older_than(self, ts: datetime) -> None:
        cutoff = window_start(ts, self.window_s) - timedelta(seconds=2 * self.window_s)
        for key in [k for k in self._buckets if k[1] < cutoff]:
            del self._buckets[key]

    def __len__(self) -> int:
        return len(self._buckets)


# ── anomaly detection ────────────────────────────────────────────────────────
# Absolute scale floors per feature: zero-inflated features (fracs, error rate)
# have MAD ~0, so tiny deviations would otherwise explode the robust z-score.
_FEATURE_FLOORS = np.array([0.10, 10.0, 10.0, 0.3, 1.0, 0.15, 0.10, 0.10])


class AnomalyDetector:
    """Per-service hybrid anomaly detector over sliding-window feature vectors.

    score = max(
      IsolationForest (multivariate; needs a decent baseline),
      robust per-feature z-score (median/MAD; reliable from ~8 windows),
    ), squashed into [0, 1]: healthy ~0.05, anomalous > threshold.

    - contaminated windows are scored but never enter the training buffer;
    - `effective_threshold` raises the static threshold to the 99.5th percentile
      of clean sub-threshold score history (adapts to noisy baselines, and can
      never self-poison because flagged scores are excluded from history);
    - the median clean p95 is tracked as a baseline for drift forecasting.
    """

    ROBUST_MIN = 8  # clean windows required before the robust component activates

    def __init__(self, contamination: float = 0.02, min_train: int = 30,
                 retrain_every: int = 50, max_buffer: int = 1500) -> None:
        self.contamination = contamination
        self.min_train = min_train
        self.retrain_every = retrain_every
        self._clean: deque[np.ndarray] = deque(maxlen=max_buffer)
        self._clean_scores: deque[float] = deque(maxlen=200)
        self._clean_p95: deque[float] = deque(maxlen=300)
        self._since_fit = retrain_every  # trigger the first fit as soon as min_train is reached
        self.model: IsolationForest | None = None
        self.mu_ = 0.0
        self.sigma_ = 1.0

    @property
    def fitted(self) -> bool:
        return self.model is not None

    @property
    def baseline_size(self) -> int:
        return len(self._clean)

    def observe(self, x: np.ndarray, contaminated: bool = False,
                p95_ms: float | None = None, static_threshold: float = 1.0) -> float | None:
        """Feed one window; returns anomaly score in [0, 1], or None while cold."""
        x = np.asarray(x, dtype=np.float64)
        if not contaminated:
            self._clean.append(x)
        if len(self._clean) >= self.min_train and (self.model is None or self._since_fit >= self.retrain_every):
            self._refit()
        if self.model is None and len(self._clean) < self.ROBUST_MIN:
            return None
        if_score = self._score(x) if self.model is not None else 0.0
        robust_score = self._robust_score(x) if len(self._clean) >= self.ROBUST_MIN else 0.0
        score = max(if_score, robust_score)
        if not contaminated:
            self._since_fit += 1
            if score < static_threshold:
                self._clean_scores.append(score)
            if p95_ms is not None:
                self._clean_p95.append(p95_ms)
        return score

    def effective_threshold(self, static: float) -> float:
        if len(self._clean_scores) < 50:
            return static
        return max(static, float(np.quantile(np.asarray(self._clean_scores), 0.995)))

    def p95_baseline(self) -> float:
        if len(self._clean_p95) < 20:
            return 0.0
        return float(np.median(np.asarray(self._clean_p95)))

    def _refit(self) -> None:
        X = np.vstack(self._clean)
        self.model = IsolationForest(
            n_estimators=150, contamination=self.contamination,
            random_state=17, n_jobs=-1,
        ).fit(X)
        raw = -self.model.score_samples(X)  # higher = more anomalous
        self.mu_ = float(raw.mean())
        self.sigma_ = max(float(raw.std()), 1e-6)
        self._since_fit = 0

    def _score(self, x: np.ndarray) -> float:
        raw = float(-self.model.score_samples(x.reshape(1, -1))[0])
        z = (raw - self.mu_) / self.sigma_
        return 1.0 / (1.0 + math.exp(-(1.2 * z - 3.0)))

    def _robust_score(self, x: np.ndarray) -> float:
        """Per-feature robust z (median/MAD + sane floors) — gross deviations only.

        Fires when a window deviates strongly in at least one feature or
        moderately in several at once; immune to single-feature baseline noise
        (one rare 5xx in an otherwise quiet window must not flag).
        """
        X = np.asarray(self._clean, dtype=np.float64)
        med = np.median(X, axis=0)
        mad = np.median(np.abs(X - med), axis=0)
        scale = np.maximum(1.4826 * mad, _FEATURE_FLOORS)
        z = np.abs(x - med) / scale
        max_z = float(z.max())
        multi_feature = float(np.sum(np.maximum(z - 4.0, 0.0)))  # simultaneous deviations
        # a single feature must reach z>=4 to flag alone (z=4 -> 0.65, z=5 -> 0.86);
        # p95 tails in sparse windows sit around z=2-3 and must stay silent
        single = 1.0 / (1.0 + math.exp(-(1.2 * max_z - 4.2)))
        return max(single, min(1.0, multi_feature / 15.0))


# ── log clustering ───────────────────────────────────────────────────────────
class ClusterAssignment(NamedTuple):
    fingerprint: str
    template: str
    confidence: float  # cosine similarity to the winning centroid (1.0 = exact)
    created: bool


class LogClusterer:
    """Streaming TF-IDF + cosine leader clustering.

    A normalized template either joins the nearest cluster centroid
    (cosine >= similarity) or seeds a new one; centroids are incremental
    directional means. The vectorizer bootstraps on the first `min_boot`
    unique templates and is refit on the rolling corpus every `refit_every`
    assignments; near-duplicate clusters (cosine >= 0.85) are merged on refit.
    """

    MERGE_SIMILARITY = 0.75  # near-duplicate templates differing by ~1 token score ~0.75-0.90

    def __init__(self, similarity: float = 0.60, refit_every: int = 120,
                 min_boot: int = 30, max_corpus: int = 3000, max_members: int = 24) -> None:
        self.similarity = similarity
        self.refit_every = refit_every
        self.min_boot = min_boot
        self.max_corpus = max_corpus
        self.max_members = max_members
        self._vec: TfidfVectorizer | None = None
        self._centroids: dict[str, csr_matrix] = {}
        self._counts: dict[str, int] = {}
        self._members: dict[str, list[str]] = {}
        self._corpus: dict[str, None] = {}  # insertion-ordered set
        self._pending: list[str] = []       # cold-start buffer
        self._pending_seen = 0
        self._n_seen = 0

    @property
    def warmed(self) -> bool:
        return self._vec is not None

    @property
    def cluster_count(self) -> int:
        return len(self._centroids)

    @property
    def corpus_size(self) -> int:
        return len(self._corpus)

    def assign(self, message: str) -> ClusterAssignment:
        template = normalize_message(message)
        own_fp = fingerprint(template)
        self._n_seen += 1
        if self._vec is None:
            self._pending_seen += 1
            if template not in self._pending:
                self._pending.append(template)
            # warm on enough unique templates, or on sheer volume (low-cardinality streams)
            if len(self._pending) >= self.min_boot or self._pending_seen >= self.min_boot * 10:
                self._refit(corpus=list(self._pending))
                self._pending.clear()
            else:
                return ClusterAssignment(own_fp, template, 0.0, False)
        elif (template not in self._corpus and len(self._corpus) < 50) \
                or self._n_seen % self.refit_every == 0:
            # young corpus: refit on every unseen template so early similarities
            # are computed against a near-exact vocabulary (prevents degenerate merges)
            self._refit(extra=template if template not in self._corpus else None)
        v = self._unit(self._vec.transform([template]))
        created = False
        fp, confidence = own_fp, 1.0
        if own_fp not in self._centroids:
            near_fp, sim = self._nearest(v)
            if near_fp is not None and sim >= self.similarity:
                fp, confidence = near_fp, sim
            else:
                self._centroids[own_fp] = v.copy()
                self._counts[own_fp] = 1
                self._members[own_fp] = [template]
                created = True
        self._remember(fp, template, v)
        return ClusterAssignment(fp, template, confidence, created)

    def _remember(self, fp: str, template: str, v: csr_matrix) -> None:
        n = self._counts[fp] + 1
        self._counts[fp] = n
        self._centroids[fp] = self._unit((self._centroids[fp] * (n - 1) + v) / n)
        members = self._members[fp]
        if template not in members:
            members.append(template)
            if len(members) > self.max_members:
                members.pop(1)  # keep the founding template
        self._corpus.setdefault(template, None)
        while len(self._corpus) > self.max_corpus:
            self._corpus.pop(next(iter(self._corpus)))

    def _refit(self, corpus: list[str] | None = None, extra: str | None = None) -> None:
        docs = list(self._corpus) if corpus is None else list(corpus)
        if extra and extra not in docs:
            docs.append(extra)
        if not docs:
            return
        self._vec = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)
        self._vec.fit(docs)
        self._centroids = {
            fp: self._unit(csr_matrix(np.asarray(self._vec.transform(templates).mean(axis=0))))
            for fp, templates in self._members.items()
        }
        self._merge_similar()

    def _merge_similar(self) -> None:
        fps = list(self._centroids)
        for i, a in enumerate(fps):
            for b in fps[i + 1:]:
                if a not in self._centroids or b not in self._centroids:
                    continue  # already merged away
                sim = float(self._centroids[a].multiply(self._centroids[b]).sum())
                if sim >= self.MERGE_SIMILARITY:
                    keep, drop = (a, b) if self._counts[a] >= self._counts[b] else (b, a)
                    self._merge(keep, drop)

    def _merge(self, keep: str, drop: str) -> None:
        self._counts[keep] += self._counts.pop(drop)
        del self._centroids[drop]
        members = self._members[keep]
        for t in self._members.pop(drop):
            if t not in members:
                members.append(t)
        excess = len(members) - self.max_members
        if excess > 0:
            del members[1:1 + excess]
        V = self._vec.transform(members)
        self._centroids[keep] = self._unit(csr_matrix(np.asarray(V.mean(axis=0))))

    def _nearest(self, v: csr_matrix) -> tuple[str | None, float]:
        best_fp, best_sim = None, 0.0
        for fp, centroid in self._centroids.items():
            sim = float(v.multiply(centroid).sum())
            if sim > best_sim:
                best_fp, best_sim = fp, sim
        return best_fp, best_sim

    @staticmethod
    def _unit(v: Any) -> csr_matrix:
        v = csr_matrix(v)
        norm = math.sqrt(v.multiply(v).sum())
        return v / norm if norm > 0 else v
