import type { EngineStatus, Incident, IncidentDetail, InjectResponse, ServiceSummary, WindowPoint } from "./types";

const BASE = (import.meta.env.VITE_API_URL as string | undefined) ?? "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "content-type": "application/json" },
    ...init,
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const api = {
  engines: () => req<EngineStatus>("/api/engines"),
  timeseries: (minutes: number) => req<WindowPoint[]>(`/api/metrics/timeseries?minutes=${minutes}`),
  services: () => req<ServiceSummary[]>("/api/services"),
  incidents: () => req<Incident[]>("/api/incidents?limit=200"),
  incident: (id: number) => req<IncidentDetail>(`/api/incidents/${id}`),
  setStatus: (id: number, status: string) =>
    req<Incident>(`/api/incidents/${id}/status`, { method: "POST", body: JSON.stringify({ status }) }),
  feedback: (id: number, verdict: "confirmed" | "rejected", actualCause: string | null) =>
    req<Incident>(`/api/incidents/${id}/feedback`, {
      method: "POST",
      body: JSON.stringify({ verdict, actual_cause: actualCause }),
    }),
  notes: (id: number, notes: string) =>
    req<Incident>(`/api/incidents/${id}/notes`, { method: "POST", body: JSON.stringify({ notes }) }),
  inject: (body: object) =>
    req<InjectResponse>("/api/simulate/incident", { method: "POST", body: JSON.stringify(body) }),
};

export const postmortemUrl = (id: number) => `${BASE}/api/incidents/${id}/postmortem`;

const STREAM_EVENTS = ["window_anomaly", "drift", "incident_created", "incident_triaged"];

/** Live SSE subscription; calls onEvent("sse_error") when the stream drops (caller falls back to polling). */
export function subscribe(onEvent: (type: string) => void): () => void {
  const es = new EventSource(`${BASE}/api/stream`);
  for (const type of STREAM_EVENTS) {
    es.addEventListener(type, () => onEvent(type));
  }
  es.onerror = () => onEvent("sse_error");
  return () => es.close();
}
