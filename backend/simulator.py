"""Synthetic telemetry + chaos generator.

Normal traffic mimics a small e-commerce microservice fleet. `inject()` schedules
failure scenarios (latency spike, memory leak, cascading DB pool exhaustion /
circuit-breaker cascade) that ramp in over N ticks, then expire.

CLI:      python -m backend.simulator --ticks 240 --inject db_pool_exhaustion@80
Library:  TelemetrySimulator.tick() from a FastAPI startup task.
"""
from __future__ import annotations

import argparse
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .database import get_session, init_db
from .pipeline import analyze_cycle, ingest

SERVICES = (
    "api-gateway", "auth-service", "orders-service", "payments-service",
    "inventory-service", "postgres-primary", "redis-cache",
)

SCENARIO_KINDS = ("latency_spike", "memory_leak", "db_pool_exhaustion", "cascade_failure")
DEFAULT_SERVICE = {
    "latency_spike": "payments-service",
    "memory_leak": "orders-service",
    "db_pool_exhaustion": "postgres-primary",
    "cascade_failure": "postgres-primary",
}

DB_DEPENDENTS = ("orders-service", "payments-service", "auth-service", "inventory-service")
DOWNSTREAM_OF_DB = DB_DEPENDENTS + ("api-gateway",)

PROFILE_LATENCY_MS = {
    "api-gateway": 18, "auth-service": 24, "orders-service": 42,
    "payments-service": 95, "inventory-service": 30,
    "postgres-primary": 8, "redis-cache": 2,
}

# (weight, level, status_code, template) — placeholders: {ms} {hex} {ip} {num}
BASE_TRAFFIC: dict[str, list[tuple[int, str, int | None, str]]] = {
    "api-gateway": [
        (55, "INFO", 200, "GET /v1/orders 200 {ms}ms upstream=orders-service trace={hex}"),
        (15, "INFO", 201, "POST /v1/checkout 201 {ms}ms upstream=orders-service trace={hex}"),
        (10, "INFO", 200, "GET /v1/inventory/stock/{num} 200 {ms}ms upstream=inventory-service"),
        (8, "INFO", 200, "POST /v1/auth/token 200 {ms}ms upstream=auth-service"),
        (5, "WARN", 401, "POST /v1/auth/token 401 {ms}ms upstream=auth-service reason=invalid_grant"),
        (4, "WARN", 404, "GET /v1/orders/{hex} 404 {ms}ms upstream=orders-service"),
        (2, "WARN", 429, "GET /v1/orders 429 {ms}ms reason=rate_limited key={ip}"),
        (1, "ERROR", 502, "GET /v1/checkout 502 {ms}ms upstream=payments-service error=upstream_timeout"),
    ],
    "auth-service": [
        (50, "INFO", 200, "token issued grant=client_credentials sub={hex} ttl=3600s"),
        (18, "INFO", 200, "token refreshed sub={hex} scope=orders.read inventory.read"),
        (12, "INFO", 200, "jwt validated sub={hex} aud=api algo=RS256"),
        (10, "WARN", 401, "auth failed sub=anonymous reason=bad_credentials ip={ip}"),
        (6, "WARN", 429, "rate limited key={ip} limit=100 per window"),
        (4, "ERROR", 500, "keyring backend unavailable, falling back to env provider"),
    ],
    "orders-service": [
        (40, "INFO", 201, "order created id=ord_{hex} items={num} total=${num}"),
        (18, "INFO", 200, "order state transition id=ord_{hex} pending->confirmed"),
        (14, "INFO", 200, "inventory reservation ok id=ord_{hex} sku=SKU-{num}"),
        (12, "INFO", 200, "order fetched id=ord_{hex} cache=hit"),
        (8, "WARN", 409, "idempotency conflict id=ord_{hex} key={hex}"),
        (4, "ERROR", 500, "payment declined id=ord_{hex} reason=insufficient_funds"),
        (4, "ERROR", 500, "unhandled exception in checkout flow id=ord_{hex}: TimeoutError: upstream did not respond within 3000ms"),
    ],
    "payments-service": [
        (45, "INFO", 200, "charge authorized id=ch_{hex} amount=${num} brand=visa"),
        (16, "INFO", 200, "charge captured id=ch_{hex} amount=${num}"),
        (12, "INFO", 200, "refund issued id=re_{hex} amount=${num}"),
        (10, "WARN", 402, "3ds challenge required id=ch_{hex}"),
        (9, "WARN", 503, "processor degraded id=ch_{hex} latency={ms}ms retrying=1"),
        (8, "ERROR", 500, "processor hard decline id=ch_{hex} code=do_not_honor"),
    ],
    "inventory-service": [
        (42, "INFO", 200, "stock reserved sku=SKU-{num} qty={num}"),
        (20, "INFO", 200, "stock released sku=SKU-{num} qty={num}"),
        (16, "INFO", 200, "reorder point check sku=SKU-{num} on_hand={num}"),
        (12, "WARN", 409, "oversell blocked sku=SKU-{num} requested={num}"),
        (10, "WARN", 200, "low stock sku=SKU-{num} remaining={num}"),
    ],
    "postgres-primary": [
        (38, "INFO", None, "duration: {ms} ms  statement: SELECT id, status FROM orders WHERE customer_id = {num}"),
        (20, "INFO", None, "checkpoint complete: wrote {num} buffers ({num}% full), wrote {num} WAL files"),
        (14, "INFO", None, "connection authorized: user=orders_app database=orders pid={num}"),
        (12, "INFO", None, 'autovacuum of "public.orders": removed {num} row versions'),
        (10, "WARN", None, "slow query: duration: {ms} ms  statement: SELECT * FROM order_items WHERE order_id = {num}"),
        (6, "ERROR", None, "deadlock detected: process {num} waits for ShareLock on transaction {hex}"),
    ],
    "redis-cache": [
        (40, "INFO", None, "DB saved on disk: {num} MB in {num} s"),
        (24, "INFO", None, "key expired db=0 key=session:{hex}"),
        (22, "INFO", None, "GET cache:orders:{hex} hit ({num} us)"),
        (14, "WARN", None, "evicted key cache:cart:{hex} maxmemory-policy=allkeys-lru"),
    ],
}

_LATENCY_FACTOR = {
    "latency_spike": 9.0, "memory_leak": 2.5,
    "db_pool_exhaustion": 5.0, "cascade_failure": 5.0,
}


@dataclass
class Scenario:
    kind: str
    service: str
    total_ticks: int
    ticks_left: int
    intensity: float = 1.0

    @property
    def ramp(self) -> float:
        return min(1.0, 1.0 - (self.ticks_left / self.total_ticks))


class TelemetrySimulator:
    def __init__(self, interval: float | None = None, seed: int | None = None) -> None:
        self.interval = interval if interval is not None else get_settings().simulate_interval_s
        self.step = get_settings().sim_step_s
        self.rng = random.Random(seed)
        self.clock = datetime.now(timezone.utc).replace(tzinfo=None)
        self.scenarios: list[Scenario] = []
        self.finished: list[Scenario] = []

    def inject(self, kind: str, service: str | None = None, ticks: int = 20, intensity: float = 1.0) -> Scenario:
        if kind not in SCENARIO_KINDS:
            raise ValueError(f"unknown scenario {kind!r}; expected one of {SCENARIO_KINDS}")
        sc = Scenario(kind=kind, service=service or DEFAULT_SERVICE[kind],
                      total_ticks=ticks, ticks_left=ticks, intensity=intensity)
        self.scenarios.append(sc)
        return sc

    def tick(self) -> list[dict]:
        """Advance one tick and emit a batch of telemetry dicts."""
        self.clock += timedelta(seconds=self.step)
        events: list[dict] = []
        for svc in SERVICES:
            scens = self._scenarios_for(svc)
            tag = scens[0].kind if scens else None
            events += self._normal_traffic(svc, self._latency_multiplier(scens), tag)
            for sc in scens:
                if sc.service == svc:
                    events += self._scenario_lines(sc)
        for sc in self.scenarios:
            sc.ticks_left -= 1
        self.finished = [sc for sc in self.scenarios if sc.ticks_left <= 0]
        self.scenarios = [sc for sc in self.scenarios if sc.ticks_left > 0]
        return events

    # ── internals ────────────────────────────────────────────────────────────
    def _scenarios_for(self, svc: str) -> list[Scenario]:
        out = [sc for sc in self.scenarios if sc.service == svc]
        if svc in DOWNSTREAM_OF_DB:
            out += [sc for sc in self.scenarios
                    if sc.kind in ("db_pool_exhaustion", "cascade_failure") and sc not in out]
        return out

    def _latency_multiplier(self, scens: list[Scenario]) -> float:
        mult = 1.0
        for sc in scens:
            mult = max(mult, 1.0 + _LATENCY_FACTOR[sc.kind] * sc.intensity * sc.ramp)
        return mult

    def _normal_traffic(self, svc: str, mult: float, tag: str | None) -> list[dict]:
        pool = BASE_TRAFFIC[svc]
        out = []
        for _ in range(self.rng.randint(3, 8)):
            _, level, status, tpl = self.rng.choices(pool, weights=[row[0] for row in pool], k=1)[0]
            lat = self.rng.lognormvariate(0.0, 0.35) * PROFILE_LATENCY_MS[svc] * mult
            out.append(self._event(svc, level, status, tpl, latency_ms=round(lat, 1), scenario=tag))
        return out

    def _scenario_lines(self, sc: Scenario) -> list[dict]:
        if sc.kind == "latency_spike":
            return self._latency_spike_lines(sc)
        if sc.kind == "memory_leak":
            return self._memory_leak_lines(sc)
        return self._db_pool_lines(sc)  # db_pool_exhaustion | cascade_failure

    def _latency_spike_lines(self, sc: Scenario) -> list[dict]:
        p95 = int(300 + 1400 * sc.ramp * sc.intensity)
        out = [
            self._event(sc.service, "WARN", None,
                        f"upstream latency elevated svc={sc.service} p95={p95}ms "
                        f"retry_budget={max(0, 100 - int(sc.ramp * 130))}%", scenario=sc.kind)
            for _ in range(self.rng.randint(1, 2))
        ]
        if sc.ramp > 0.6:
            out.append(self._event(sc.service, "ERROR", None,
                                   f"latency SLO burned: svc={sc.service} p95={p95}ms budget=300ms severity=critical",
                                   scenario=sc.kind))
        if sc.ramp > 0.8:
            out.append(self._event("api-gateway", "ERROR", 504,
                                   f"GET /v1/checkout 504 {int(1400 + 900 * sc.ramp)}ms upstream={sc.service} error=upstream_timeout",
                                   scenario=sc.kind))
        return out

    def _memory_leak_lines(self, sc: Scenario) -> list[dict]:
        heap = min(99.0, 30 + 65 * sc.ramp * sc.intensity)
        out = [self._event(sc.service, "WARN" if heap < 85 else "ERROR", None,
                           f"gc pause {int(40 + 280 * sc.ramp)}ms heap={heap:.0f}% "
                           f"live_objects={int(1.2e6 * (1 + 3 * sc.ramp))}", scenario=sc.kind)]
        if heap >= 88:
            out.append(self._event(sc.service, "ERROR", None,
                                   "MemoryError: unable to allocate 2.4 GiB, worker pool terminating",
                                   scenario=sc.kind))
        if sc.ramp >= 0.95:
            out.append(self._event(sc.service, "ERROR", None,
                                   f"kernel: oom-killer invoked, task={sc.service} killed rss=3812MB score=990",
                                   scenario=sc.kind))
        return out

    def _db_pool_lines(self, sc: Scenario) -> list[dict]:
        out = [self._event("postgres-primary", "ERROR", None,
                           "FATAL: sorry, too many clients already", scenario=sc.kind)
               for _ in range(self.rng.randint(2, 4))]
        timeout = int(30000 * (0.5 + sc.ramp))
        for dep in DB_DEPENDENTS:
            for _ in range(self.rng.randint(1, 3)):
                out.append(self._event(dep, "ERROR", 500,
                                       f"sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) "
                                       f"connection pool exhausted, timeout: {timeout} ms", scenario=sc.kind))
            if sc.kind == "cascade_failure" and sc.ramp > 0.35:
                out.append(self._event(dep, "ERROR", None,
                                       f"circuit breaker OPEN for postgres-primary after "
                                       f"{self.rng.randint(8, 40)} consecutive failures, cooling down 30s",
                                       scenario=sc.kind))
        if sc.kind == "cascade_failure" and sc.ramp > 0.5:
            out.append(self._event("api-gateway", "ERROR", 503,
                                   f"POST /v1/checkout 503 {int(900 + 700 * sc.ramp)}ms error=circuit_open upstream=payments-service",
                                   scenario=sc.kind))
        return out

    def _event(self, svc: str, level: str, status: int | None, template: str,
               latency_ms: float | None = None, scenario: str | None = None) -> dict:
        body = template.format(
            ms=str(int(latency_ms)) if latency_ms is not None else str(self.rng.randint(2, 900)),
            hex=uuid.uuid4().hex[:12],
            num=str(self.rng.randint(2, 9000)),
            ip=f"10.{self.rng.randint(0, 255)}.{self.rng.randint(0, 255)}.{self.rng.randint(1, 254)}",
        )
        attrs = {"env": "prod", "region": "eu-central-1"}
        if scenario:
            attrs["scenario"] = scenario
        ts = self.clock + timedelta(seconds=self.rng.uniform(0, self.step))
        return {
            "ts": ts.replace(tzinfo=None), "service": svc, "level": level,
            "status_code": status, "latency_ms": latency_ms, "message": body,
            "trace_id": uuid.uuid4().hex[:16], "attributes": attrs,
        }


# ── CLI ──────────────────────────────────────────────────────────────────────
def _parse_injections(specs: list[str]) -> dict[int, list[tuple[str, str | None]]]:
    plan: dict[int, list[tuple[str, str | None]]] = {}
    for spec in specs:
        kind, _, rest = spec.partition("@")
        tick_s, _, svc = rest.partition(":")
        if not tick_s.isdigit() or not kind:
            raise SystemExit(f"--inject expects KIND@TICK[:SERVICE], got {spec!r}")
        plan.setdefault(int(tick_s), []).append((kind, svc or None))
    return plan


def _print_summary(tick_no: int, n_events: int, summary: dict) -> None:
    detectors = summary["detectors"]
    fitted = sum(1 for d in detectors.values() if d["fitted"])
    print(
        f"tick {tick_no:04d} | events {n_events:3d} | windows {summary['windows']:2d} "
        f"| anomalies {summary['anomalies']:2d} | incidents {summary['incident_count']:3d} "
        f"(+{len(summary['new_incidents'])}) | isolation-forest {fitted}/{len(detectors)} fitted"
    )
    for inc in summary["new_incidents"]:
        print(f"   └ incident #{inc['id']:<3} [{inc['service']}] {inc['level']:<5} "
              f"conf={inc['confidence']:.2f}  {inc['title'][:84]}")


def main() -> None:
    p = argparse.ArgumentParser(description="TraceForge synthetic telemetry & chaos simulator")
    p.add_argument("--ticks", type=int, default=240)
    p.add_argument("--interval", type=float, default=None, help="wall-clock seconds per tick (default: SIMULATE_INTERVAL_S)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--inject", action="append", default=[], metavar="KIND@TICK[:SERVICE]",
                   help="e.g. db_pool_exhaustion@60:postgres-primary")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args()

    init_db()
    sim = TelemetrySimulator(
        interval=args.interval if args.interval is not None else get_settings().simulate_interval_s,
        seed=args.seed,
    )
    plan = _parse_injections(args.inject)
    db = get_session()
    try:
        for tick_no in range(1, args.ticks + 1):
            for kind, svc in plan.get(tick_no, []):
                sc = sim.inject(kind, service=svc)
                print(f"[chaos] tick {tick_no:04d} -> {sc.kind} on {sc.service} for {sc.total_ticks} ticks")
            events = sim.tick()
            ingest(db, events)
            summary = analyze_cycle(db)
            if not args.quiet:
                _print_summary(tick_no, len(events), summary)
            time.sleep(max(0.0, args.interval))
        print("\nsim complete -> traceforge.db (WAL).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
