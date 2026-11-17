import { useState } from "react";
import { Zap } from "lucide-react";
import { api } from "../api";
import { Panel } from "./ui";

const KINDS = ["db_pool_exhaustion", "cascade_failure", "latency_spike", "memory_leak"] as const;
const SERVICES = [
  "api-gateway", "auth-service", "orders-service", "payments-service",
  "inventory-service", "postgres-primary", "redis-cache",
];

const selectCls =
  "w-full rounded-sm border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-[11px] text-zinc-300 focus:border-zinc-600 focus:outline-none";

export function ChaosControls() {
  const [kind, setKind] = useState<(typeof KINDS)[number]>(KINDS[0]);
  const [service, setService] = useState("");
  const [ticks, setTicks] = useState(20);
  const [intensity, setIntensity] = useState(1);
  const [ff, setFf] = useState(true);
  const [busy, setBusy] = useState(false);
  const [logLine, setLogLine] = useState<string | null>(null);

  const inject = async () => {
    setBusy(true);
    try {
      const res = await api.inject({ kind, service: service || null, ticks, intensity, fast_forward: ff });
      setLogLine(
        `→ ${res.scenario.kind}@${res.scenario.service} · ${res.events_generated} events · ` +
        `+${res.new_incidents.length} incidents${res.fast_forwarded ? "" : " · queued"}`,
      );
    } catch (err) {
      setLogLine(`→ injection failed: ${err}`);
    }
    setBusy(false);
  };

  return (
    <Panel title="CHAOS INJECTION">
      <div className="space-y-3 px-3.5 py-3">
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="mb-1 block font-mono text-[9px] tracking-[0.15em] text-zinc-600">SCENARIO</span>
            <select value={kind} onChange={(e) => setKind(e.target.value as (typeof KINDS)[number])} className={selectCls}>
              {KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
          </label>
          <label className="block">
            <span className="mb-1 block font-mono text-[9px] tracking-[0.15em] text-zinc-600">SERVICE</span>
            <select value={service} onChange={(e) => setService(e.target.value)} className={selectCls}>
              <option value="">auto</option>
              {SERVICES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </label>
        </div>
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="mb-1 block font-mono text-[9px] tracking-[0.15em] text-zinc-600">DURATION · {ticks} TICKS</span>
            <input type="range" min={4} max={60} step={2} value={ticks}
                   onChange={(e) => setTicks(Number(e.target.value))} className="w-full accent-zinc-400" />
          </label>
          <label className="block">
            <span className="mb-1 block font-mono text-[9px] tracking-[0.15em] text-zinc-600">INTENSITY · {intensity.toFixed(1)}x</span>
            <input type="range" min={0.5} max={3} step={0.5} value={intensity}
                   onChange={(e) => setIntensity(Number(e.target.value))} className="w-full accent-zinc-400" />
          </label>
        </div>
        <label className="flex items-center gap-2 font-mono text-[10px] text-zinc-400">
          <input type="checkbox" checked={ff} onChange={(e) => setFf(e.target.checked)} className="accent-zinc-400" />
          fast-forward (materialize immediately)
        </label>
        <button onClick={inject} disabled={busy}
          className="flex w-full items-center justify-center gap-2 rounded-sm bg-zinc-100 py-1.5 font-mono text-[11px] font-medium tracking-[0.12em] text-zinc-950 hover:bg-white disabled:opacity-40">
          <Zap className="h-3.5 w-3.5" strokeWidth={2} /> INJECT INCIDENT
        </button>
        {logLine && <div className="break-all font-mono text-[10px] text-zinc-500">{logLine}</div>}
      </div>
    </Panel>
  );
}
