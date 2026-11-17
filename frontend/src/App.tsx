import { Suspense, lazy, useCallback, useEffect, useRef, useState } from "react";
import { api, subscribe } from "./api";
import { ChaosControls } from "./components/ChaosControls";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { IncidentDetailPanel } from "./components/IncidentDetailPanel";
import { IncidentFeed } from "./components/IncidentFeed";
import { SystemStrip } from "./components/SystemStrip";
import { TopBar } from "./components/TopBar";
import type { EngineStatus, Incident, IncidentDetail, WindowPoint } from "./types";

const TelemetryCharts = lazy(() => import("./components/TelemetryCharts"));

const POLL_MS = 4000;

export default function App() {
  const [engines, setEngines] = useState<EngineStatus | null>(null);
  const [points, setPoints] = useState<WindowPoint[]>([]);
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selectedId, setSelectedIdState] = useState<number | null>(() => {
    const p = new URLSearchParams(window.location.search).get("incident");
    return p ? Number(p) : null;
  });
  const [detail, setDetail] = useState<IncidentDetail | null>(null);
  const [range, setRange] = useState(60);
  const [error, setError] = useState<string | null>(null);
  const [sseOk, setSseOk] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [e, ts, inc] = await Promise.all([api.engines(), api.timeseries(range), api.incidents()]);
      setEngines(e);
      setPoints(ts);
      setIncidents(inc);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }, [range]);

  const refreshRef = useRef(refresh);
  refreshRef.current = refresh;

  // polling safety net + refetch on range change
  useEffect(() => {
    refreshRef.current();
    const timer = setInterval(() => refreshRef.current(), POLL_MS);
    return () => clearInterval(timer);
  }, [range]);

  // SSE push: immediate debounced refresh; falls back to polling on stream errors
  useEffect(() => {
    let last = 0;
    return subscribe((type) => {
      if (type === "sse_error") {
        setSseOk(false);
        return;
      }
      setSseOk(true);
      const now = Date.now();
      if (now - last > 300) {
        last = now;
        refreshRef.current();
      }
    });
  }, []);

  const setSelectedId = useCallback((id: number | null) => {
    setSelectedIdState(id);
    const url = new URL(window.location.href);
    if (id == null) url.searchParams.delete("incident");
    else url.searchParams.set("incident", String(id));
    window.history.replaceState(null, "", url);
  }, []);

  // keyboard navigation: j/k move through the feed, esc closes the detail panel
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement
          || e.target instanceof HTMLSelectElement) return;
      if (e.key === "Escape") {
        setSelectedId(null);
      } else if (e.key === "j" || e.key === "k") {
        e.preventDefault();
        if (incidents.length === 0) return;
        const idx = incidents.findIndex((i) => i.id === selectedId);
        if (idx === -1) {
          setSelectedId(incidents[0].id);
        } else {
          const next = e.key === "j" ? Math.min(incidents.length - 1, idx + 1) : Math.max(0, idx - 1);
          setSelectedId(incidents[next].id);
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [incidents, selectedId, setSelectedId]);

  useEffect(() => {
    if (selectedId == null) {
      setDetail(null);
      return;
    }
    let alive = true;
    api.incident(selectedId)
      .then((d) => { if (alive) setDetail(d); })
      .catch(() => { if (alive) setDetail(null); });
    return () => { alive = false; };
  }, [selectedId, incidents]);

  const handleStatus = useCallback(async (id: number, status: string) => {
    await api.setStatus(id, status);
    await refreshRef.current();
  }, []);

  const handleFeedback = useCallback(async (id: number, verdict: "confirmed" | "rejected", cause: string | null) => {
    await api.feedback(id, verdict, cause);
    await refreshRef.current();
  }, []);

  const handleNotes = useCallback(async (id: number, notes: string) => {
    await api.notes(id, notes);
    await refreshRef.current();
  }, []);

  return (
    <div className="flex min-h-screen flex-col">
      <TopBar engines={engines} sseOk={sseOk} />
      {error && (
        <div className="border-b border-red-950 bg-red-950/30 px-4 py-1 font-mono text-[11px] text-red-400">
          api unreachable: {error}
        </div>
      )}
      <main className="mx-auto flex w-full max-w-[1500px] flex-1 flex-col gap-3 p-3 xl:flex-row">
        <div className="flex min-w-0 flex-1 flex-col gap-3">
          <SystemStrip engines={engines} />
          <ErrorBoundary>
            <Suspense fallback={
              <div className="rounded-sm border border-zinc-800 bg-zinc-900/30 px-3 py-2 font-mono text-[10px] text-zinc-600">
                loading charts...
              </div>
            }>
              <TelemetryCharts points={points} range={range} onRange={setRange} />
            </Suspense>
          </ErrorBoundary>
          <IncidentFeed incidents={incidents} selectedId={selectedId} onSelect={setSelectedId} />
        </div>
        <div className="flex w-full shrink-0 flex-col gap-3 xl:w-[400px]">
          <ErrorBoundary>
            <IncidentDetailPanel detail={detail} engines={engines}
                                 onStatus={handleStatus} onFeedback={handleFeedback} onNotes={handleNotes} />
          </ErrorBoundary>
          <ChaosControls />
        </div>
      </main>
    </div>
  );
}
