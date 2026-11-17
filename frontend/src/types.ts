export type Severity = "critical" | "high" | "medium" | "low";

export interface Triage {
  severity: Severity;
  root_cause_component: string;
  technical_explanation: string;
  remediation_steps: string[];
  suggested_patch_snippet: string;
}

export interface WindowPoint {
  ts: string;
  service: string;
  p95_latency_ms: number;
  error_rate: number;
  rps: number;
  log_entropy: number;
  anomaly_score: number;
  is_anomaly: boolean;
  contaminated: boolean;
  forecast_risk: number;
}

export interface Incident {
  id: number;
  fingerprint: string;
  service: string;
  level: string;
  status: "open" | "acknowledged" | "resolved";
  title: string;
  occurrences: number;
  ml_confidence: number;
  max_anomaly_score: number;
  first_seen: string;
  last_seen: string;
  severity: Severity | null;
  root_cause_component: string | null;
  triage_state: "pending" | "disabled" | "done" | "cached" | "error";
  triage_attempts: number;
  triage_error: string | null;
  triage: Triage | null;
  blast_radius: { services?: string[]; source?: string } | null;
  feedback: { verdict: "confirmed" | "rejected"; actual_cause?: string | null } | null;
  resolution_notes: string | null;
  resolved_at: string | null;
}

export interface EventSample {
  ts: string;
  service: string;
  level: string;
  message: string;
  status_code: number | null;
  latency_ms: number | null;
  trace_id: string;
}

export interface IncidentDetail extends Incident {
  samples: EventSample[];
  tokens: { prompt: number; completion: number } | null;
}

export interface EngineStatus {
  clusterer: { warmed: boolean; clusters: number; corpus: number };
  detectors: Record<string, { fitted: boolean; baseline_size: number; threshold: number }>;
  llm: {
    enabled: boolean;
    model: string;
    cache_entries: number;
    cache_hits: number;
    tokens_prompt: number;
    tokens_completion: number;
  };
  stats: { events_total: number; incidents_open: number; anomalies_1h: number };
}

export interface ServiceSummary {
  service: string;
  detector_fitted: boolean;
  baseline_size: number;
  open_incidents: number;
  latest: WindowPoint | null;
}

export interface InjectResponse {
  scenario: { kind: string; service: string; ticks: number; intensity: number };
  fast_forwarded: boolean;
  events_generated: number;
  new_incidents: { id: number; service: string; title: string; confidence: number }[];
  note: string;
}
