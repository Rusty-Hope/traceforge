import { Cpu } from "lucide-react";
import type { EngineStatus } from "../types";

function Led({ ok }: { ok: boolean }) {
  return <span className={`inline-block h-1.5 w-1.5 rounded-full ${ok ? "bg-emerald-500" : "bg-zinc-700"}`} />;
}

export function TopBar({ engines, sseOk }: { engines: EngineStatus | null; sseOk: boolean }) {
  const det = engines?.detectors ?? {};
  const fitted = Object.values(det).filter((d) => d.fitted).length;
  const total = Object.keys(det).length;
  return (
    <header className="flex h-12 shrink-0 items-center justify-between border-b border-zinc-800 bg-zinc-950 px-4">
      <div className="flex items-center gap-3">
        <Cpu className="h-4 w-4 text-zinc-500" strokeWidth={1.5} />
        <span className="font-mono text-[13px] font-medium tracking-[0.28em] text-zinc-100">TRACEFORGE</span>
        <span className="hidden font-mono text-[10px] text-zinc-600 md:inline">hybrid ml · background llm triage</span>
      </div>
      <div className="flex items-center gap-5 font-mono text-[10px] text-zinc-500">
        <span className="hidden items-center gap-1.5 sm:flex">
          <Led ok={engines?.clusterer.warmed ?? false} />
          TF-IDF {engines?.clusterer.clusters ?? 0} CLUSTERS
        </span>
        <span className="hidden items-center gap-1.5 md:flex">
          <Led ok={total > 0 && fitted === total} />
          IFOREST {fitted}/{total}
        </span>
        <span className="flex items-center gap-1.5">
          <Led ok={engines?.llm.enabled ?? false} />
          {engines?.llm.enabled ? `LLM ${engines.llm.model}` : "LLM OFFLINE"}
        </span>
        <span className={`hidden items-center gap-1.5 sm:flex ${sseOk ? "text-zinc-400" : "text-amber-500"}`}>
          <Led ok={sseOk} />
          {sseOk ? "SSE LIVE" : "POLLING"}
        </span>
        <span className="rounded-sm border border-zinc-800 px-1.5 py-0.5 text-zinc-400">local · sqlite/wal</span>
      </div>
    </header>
  );
}
