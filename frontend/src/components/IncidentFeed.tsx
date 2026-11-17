import { useState } from "react";
import type { Incident } from "../types";
import { fmtAge } from "../format";
import { Panel, SevChip } from "./ui";

const LLM_STATE: Record<string, { label: string; cls: string }> = {
  done: { label: "TRIAGED", cls: "border-zinc-700 text-zinc-300" },
  cached: { label: "CACHED", cls: "border-zinc-700 text-zinc-500" },
  pending: { label: "QUEUED", cls: "border-zinc-800 text-zinc-600" },
  disabled: { label: "LLM OFF", cls: "border-zinc-800 text-zinc-600" },
  error: { label: "ERROR", cls: "border-red-900/60 text-red-400" },
};

const FILTERS = ["all", "open", "acknowledged", "resolved"];

export function IncidentFeed({ incidents, selectedId, onSelect }: {
  incidents: Incident[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const [filter, setFilter] = useState("all");
  const filtered = filter === "all" ? incidents : incidents.filter((i) => i.status === filter);

  return (
    <Panel
      title="INCIDENT TRIAGE FEED"
      right={
        <div className="flex items-center gap-1">
          {FILTERS.map((f) => (
            <button key={f} onClick={() => setFilter(f)}
              className={`rounded-sm px-1.5 py-0.5 font-mono text-[9px] uppercase ${filter === f ? "bg-zinc-800 text-zinc-200" : "text-zinc-600 hover:text-zinc-400"}`}>
              {f}
            </button>
          ))}
          <span className="ml-2 font-mono text-[9px] text-zinc-600">{filtered.length}</span>
        </div>
      }
    >
      <div className="max-h-[430px] overflow-auto">
        <table className="w-full border-collapse text-left">
          <thead className="sticky top-0 z-10 bg-zinc-950">
            <tr className="border-b border-zinc-800 font-mono text-[9px] tracking-[0.12em] text-zinc-600">
              <th className="px-3 py-1.5 font-medium">SEV</th>
              <th className="px-2 py-1.5 font-medium">SERVICE</th>
              <th className="px-2 py-1.5 font-medium">CANONICAL TEMPLATE</th>
              <th className="px-2 py-1.5 text-right font-medium">OCC</th>
              <th className="px-2 py-1.5 text-right font-medium">CONF</th>
              <th className="px-2 py-1.5 text-right font-medium">ANOM</th>
              <th className="px-2 py-1.5 font-medium">LLM</th>
              <th className="px-2 py-1.5 font-medium">STATUS</th>
              <th className="px-3 py-1.5 text-right font-medium">AGE</th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 && (
              <tr>
                <td colSpan={9} className="px-3 py-8 text-center font-mono text-[11px] text-zinc-600">
                  no incidents {filter !== "all" ? `with status "${filter}"` : "yet — inject a scenario"}
                </td>
              </tr>
            )}
            {filtered.map((inc) => {
              const llm = LLM_STATE[inc.triage_state] ?? LLM_STATE.pending;
              return (
                <tr key={inc.id} onClick={() => onSelect(inc.id)}
                    className={`cursor-pointer border-b border-zinc-900 font-mono text-[11px] hover:bg-zinc-900/60 ${selectedId === inc.id ? "bg-zinc-900/70" : ""}`}>
                  <td className="px-3 py-1.5"><SevChip sev={inc.severity} state={inc.triage_state} /></td>
                  <td className="whitespace-nowrap px-2 py-1.5 text-zinc-300">{inc.service}</td>
                  <td className="max-w-[380px] truncate px-2 py-1.5 text-zinc-500" title={inc.title}>{inc.title}</td>
                  <td className="px-2 py-1.5 text-right text-zinc-300">{inc.occurrences}</td>
                  <td className="px-2 py-1.5 text-right text-zinc-500">{inc.ml_confidence.toFixed(2)}</td>
                  <td className={`px-2 py-1.5 text-right ${inc.max_anomaly_score >= 0.62 ? "text-red-400" : "text-zinc-500"}`}>
                    {inc.max_anomaly_score > 0 ? inc.max_anomaly_score.toFixed(2) : "—"}
                  </td>
                  <td className="px-2 py-1.5">
                    <span className={`inline-block rounded-sm border px-1.5 py-px text-[9px] ${llm.cls}`}>{llm.label}</span>
                  </td>
                  <td className="px-2 py-1.5 text-zinc-400">{inc.status}</td>
                  <td className="px-3 py-1.5 text-right text-zinc-600">{fmtAge(inc.last_seen)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
