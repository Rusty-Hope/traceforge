import type { EngineStatus } from "../types";
import { fmtNum } from "../format";

export function SystemStrip({ engines }: { engines: EngineStatus | null }) {
  const s = engines?.stats;
  const tokens = (engines?.llm.tokens_prompt ?? 0) + (engines?.llm.tokens_completion ?? 0);
  const cells: [string, string | number][] = [
    ["EVENTS INGESTED", s?.events_total ?? 0],
    ["ANOMALIES · 1H", s?.anomalies_1h ?? 0],
    ["OPEN INCIDENTS", s?.incidents_open ?? 0],
    ["LLM TOKENS SPENT", fmtNum(tokens)],
    ["CACHE HITS", engines?.llm.cache_hits ?? 0],
  ];
  return (
    <div className="grid grid-cols-2 divide-zinc-800 border border-zinc-800 bg-zinc-900/30 sm:grid-cols-3 sm:divide-x lg:grid-cols-5">
      {cells.map(([label, value]) => (
        <div key={label} className="px-4 py-2.5">
          <div className="font-mono text-[9px] tracking-[0.15em] text-zinc-500">{label}</div>
          <div className="mt-0.5 font-mono text-lg text-zinc-100">{value}</div>
        </div>
      ))}
    </div>
  );
}
