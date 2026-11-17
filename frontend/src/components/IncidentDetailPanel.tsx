import { Check, CheckCheck, FileDown, X } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { postmortemUrl } from "../api";
import { fmtNum } from "../format";
import type { EngineStatus, IncidentDetail } from "../types";
import { Badge, KV, Panel, Section, SevChip } from "./ui";

function MiniBtn({ children, onClick, active = false }: { children: ReactNode; onClick: () => void; active?: boolean }) {
  return (
    <button onClick={onClick}
      className={`rounded-sm border px-2 py-0.5 font-mono text-[9px] tracking-wider ${active ? "border-zinc-500 bg-zinc-800 text-zinc-100" : "border-zinc-700 text-zinc-300 hover:border-zinc-500 hover:text-zinc-100"}`}>
      {children}
    </button>
  );
}

export function IncidentDetailPanel({ detail, engines, onStatus, onFeedback, onNotes }: {
  detail: IncidentDetail | null;
  engines: EngineStatus | null;
  onStatus: (id: number, status: string) => void;
  onFeedback: (id: number, verdict: "confirmed" | "rejected", actualCause: string | null) => void;
  onNotes: (id: number, notes: string) => void;
}) {
  const [cause, setCause] = useState("");
  const [notes, setNotes] = useState("");

  useEffect(() => {
    setCause("");
    setNotes(detail?.resolution_notes ?? "");
  }, [detail?.id, detail?.resolution_notes]);

  if (!detail) {
    return (
      <Panel title="INCIDENT DETAIL">
        <div className="px-4 py-10 text-center font-mono text-[11px] text-zinc-600">
          select an incident from the feed <span className="text-zinc-700">(j / k · esc)</span>
        </div>
      </Panel>
    );
  }
  const inc = detail;
  const t = inc.triage;
  const radius = inc.blast_radius?.services ?? [];
  const tokens = detail.tokens ? detail.tokens.prompt + detail.tokens.completion : 0;

  return (
    <Panel
      title={`INCIDENT #${inc.id}`}
      right={
        <div className="flex items-center gap-1.5">
          {inc.status === "open" && <MiniBtn onClick={() => onStatus(inc.id, "acknowledged")}>ACK</MiniBtn>}
          {inc.status !== "resolved" && (
            <MiniBtn onClick={() => onStatus(inc.id, "resolved")}>
              <span className="flex items-center gap-1"><CheckCheck className="h-3 w-3" />RESOLVE</span>
            </MiniBtn>
          )}
          {inc.status === "resolved" ? (
            <a href={postmortemUrl(inc.id)} target="_blank" rel="noreferrer"
               className="flex items-center gap-1 rounded-sm border border-zinc-700 px-2 py-0.5 font-mono text-[9px] tracking-wider text-zinc-300 hover:border-zinc-500 hover:text-zinc-100">
              <FileDown className="h-3 w-3" />POST-MORTEM .MD
            </a>
          ) : (
            <span title="resolve the incident to export a post-mortem"
                  className="flex items-center gap-1 rounded-sm border border-zinc-800 px-2 py-0.5 font-mono text-[9px] tracking-wider text-zinc-600">
              <FileDown className="h-3 w-3" />POST-MORTEM .MD
            </span>
          )}
        </div>
      }
    >
      <div className="space-y-4 px-3.5 py-3">
        <div>
          <div className="font-mono text-[11px] leading-relaxed text-zinc-200">{inc.title}</div>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            <Badge>{inc.service}</Badge>
            <Badge>{inc.level}</Badge>
            <Badge>{inc.status}</Badge>
            <SevChip sev={inc.severity} state={inc.triage_state} />
          </div>
        </div>

        <Section label="ML EVIDENCE — LOCAL, ZERO API COST">
          <div className="rounded-sm border border-zinc-800/80 bg-zinc-950/40 px-3 py-1.5">
            <KV k="fingerprint (sha256)" v={`${inc.fingerprint.slice(0, 16)}...`} />
            <KV k="occurrences" v={String(inc.occurrences)} />
            <KV k="cluster confidence · tfidf cosine" v={inc.ml_confidence.toFixed(3)} />
            <KV k="max anomaly score · hybrid iforest+robust" v={inc.max_anomaly_score.toFixed(3)}
                accent={inc.max_anomaly_score >= 0.62} />
            <KV k="first seen" v={inc.first_seen.slice(11, 19)} />
            <KV k="last seen" v={inc.last_seen.slice(11, 19)} />
          </div>
        </Section>

        {radius.length > 0 && (
          <Section label={`BLAST RADIUS — CORRELATED WITH ${inc.blast_radius?.source ?? inc.service}`}>
            <div className="flex flex-wrap gap-1.5">
              {radius.map((svc) => <Badge key={svc}>{svc}</Badge>)}
            </div>
          </Section>
        )}

        <Section label={`LLM TRIAGE · ${engines?.llm.model ?? "background worker"}`}>
          {t ? (
            <div className="space-y-2.5">
              <div className="flex flex-wrap gap-1.5">
                <Badge>root cause: {t.root_cause_component}</Badge>
                <Badge>{inc.triage_state === "cached" ? "served from sha256 cache" : `fresh call · ${fmtNum(tokens)} tok`}</Badge>
              </div>
              <p className="text-[11.5px] leading-relaxed text-zinc-300">{t.technical_explanation}</p>
              <ol className="ml-4 list-decimal space-y-1 font-mono text-[10.5px] leading-relaxed text-zinc-400">
                {t.remediation_steps.map((s, i) => <li key={i}>{s}</li>)}
              </ol>
              {t.suggested_patch_snippet && (
                <pre className="overflow-x-auto rounded-sm border border-zinc-800 bg-zinc-950 p-2.5 font-mono text-[10.5px] leading-relaxed text-zinc-300">
                  {t.suggested_patch_snippet}
                </pre>
              )}
            </div>
          ) : (
            <div className="rounded-sm border border-dashed border-zinc-800 px-3 py-3 font-mono text-[10px] text-zinc-600">
              background worker: {inc.triage_state}
              {inc.triage_attempts > 0 && ` · attempts ${inc.triage_attempts}`}
              {inc.triage_error && <div className="mt-1 text-red-400/80">{inc.triage_error}</div>}
            </div>
          )}
        </Section>

        {t && (
          <Section label="HUMAN FEEDBACK — WAS THE VERDICT CORRECT?">
            {inc.feedback ? (
              <div className="flex flex-wrap gap-1.5">
                <span className={`rounded-sm border px-1.5 py-0.5 font-mono text-[9px] uppercase ${inc.feedback.verdict === "confirmed" ? "border-emerald-900/60 bg-emerald-950/30 text-emerald-400" : "border-red-900/60 bg-red-950/30 text-red-400"}`}>
                  verdict {inc.feedback.verdict}
                </span>
                {inc.feedback.actual_cause && <Badge>actual cause: {inc.feedback.actual_cause}</Badge>}
              </div>
            ) : (
              <div className="space-y-1.5">
                <div className="flex gap-1.5">
                  <MiniBtn onClick={() => onFeedback(inc.id, "confirmed", null)}>
                    <span className="flex items-center gap-1"><Check className="h-3 w-3" />CORRECT</span>
                  </MiniBtn>
                  <MiniBtn onClick={() => onFeedback(inc.id, "rejected", cause.trim() || null)}>
                    <span className="flex items-center gap-1"><X className="h-3 w-3" />WRONG</span>
                  </MiniBtn>
                </div>
                <input value={cause} onChange={(e) => setCause(e.target.value)}
                  placeholder="actual cause (optional, for rejected verdicts)"
                  className="w-full rounded-sm border border-zinc-800 bg-zinc-950 px-2 py-1 font-mono text-[10.5px] text-zinc-300 placeholder:text-zinc-700 focus:border-zinc-600 focus:outline-none" />
              </div>
            )}
          </Section>
        )}

        {inc.status === "resolved" && (
          <Section label="RESOLUTION NOTES — INCLUDED IN THE POST-MORTEM">
            <textarea value={notes} onChange={(e) => setNotes(e.target.value)} rows={3}
              placeholder="what actually happened / what fixed it"
              className="w-full resize-y rounded-sm border border-zinc-800 bg-zinc-950 px-2 py-1.5 font-mono text-[10.5px] leading-relaxed text-zinc-300 placeholder:text-zinc-700 focus:border-zinc-600 focus:outline-none" />
            <div className="mt-1.5">
              <MiniBtn onClick={() => onNotes(inc.id, notes)}>SAVE NOTES</MiniBtn>
            </div>
          </Section>
        )}

        <Section label="RAW SAMPLES">
          <div className="rounded-sm border border-zinc-800/80 bg-zinc-950/40 px-3 py-1">
            {detail.samples.slice(0, 6).map((s) => (
              <div key={s.trace_id} className="flex gap-2 border-b border-zinc-900 py-1 font-mono text-[10.5px] last:border-0">
                <span className="shrink-0 text-zinc-600">{s.ts.slice(11, 19)}</span>
                <span className={`shrink-0 ${s.level === "ERROR" ? "text-red-400/80" : "text-zinc-500"}`}>{s.level}</span>
                <span className="min-w-0 break-all text-zinc-400">{s.message}</span>
              </div>
            ))}
            {detail.samples.length === 0 && (
              <div className="py-2 text-center font-mono text-[10px] text-zinc-600">no raw samples</div>
            )}
          </div>
        </Section>
      </div>
    </Panel>
  );
}
