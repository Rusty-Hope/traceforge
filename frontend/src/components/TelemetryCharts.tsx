/* eslint-disable @typescript-eslint/no-explicit-any */
import { useEffect, useMemo, useState } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { WindowPoint } from "../types";
import { Panel } from "./ui";

const SHADES = ["#e4e4e7", "#a1a1aa", "#71717a", "#52525b", "#d4d4d8", "#3f3f46", "#fafafa"];
const RANGES: [number, string][] = [[15, "15M"], [60, "1H"], [360, "6H"], [1440, "24H"]];
const TOOLTIP = {
  background: "#18181b",
  border: "1px solid #3f3f46",
  borderRadius: 4,
  fontSize: 11,
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  padding: "6px 10px",
};
const TICK = { fill: "#71717a", fontSize: 10, fontFamily: "ui-monospace, Menlo, monospace" };

type Row = Record<string, number | string | boolean>;

const anomalyDot = (props: any): any =>
  props?.payload?.flag && props?.cx != null && props?.cy != null ? (
    <circle cx={props.cx} cy={props.cy} r={3} fill="#ef4444" stroke="#fca5a5" strokeWidth={0.5} />
  ) : null;

export default function TelemetryCharts({ points, range, onRange }: {
  points: WindowPoint[];
  range: number;
  onRange: (minutes: number) => void;
}) {
  const services = useMemo(() => [...new Set(points.map((p) => p.service))], [points]);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    setSelected((prev) => {
      if (prev.size > 0 || services.length === 0) return prev;
      const preferred = services.filter((s) => s === "api-gateway" || s === "postgres-primary");
      return new Set(preferred.length >= 2 ? preferred : services.slice(0, 2));
    });
  }, [services]);

  const rows = useMemo<Row[]>(() => {
    const byTs = new Map<string, Row>();
    for (const p of points) {
      if (!selected.has(p.service)) continue;
      const row = byTs.get(p.ts) ?? { anomaly: 0, flag: false };
      row[p.service] = p.p95_latency_ms;
      row[`${p.service}__er`] = Math.round(p.error_rate * 1000) / 10; // percent
      row.anomaly = Math.max(row.anomaly as number, p.anomaly_score);
      row.flag = (row.flag as boolean) || p.is_anomaly;
      byTs.set(p.ts, row);
    }
    return [...byTs.entries()]
      .sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([ts, row]) => ({ ...row, ts: ts.slice(11, 19) }));
  }, [points, selected]);

  const drifts = useMemo(() => {
    const latest = new Map<string, number>(); // points are ts-ascending -> last write wins
    for (const p of points) if (selected.has(p.service)) latest.set(p.service, p.forecast_risk);
    return [...latest.entries()].filter(([, risk]) => risk >= 0.5).sort((a, b) => b[1] - a[1]);
  }, [points, selected]);

  const toggle = (svc: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(svc)) next.delete(svc);
      else next.add(svc);
      return next;
    });

  return (
    <Panel
      title="TELEMETRY — P95 / ERROR RATE"
      right={
        <div className="flex items-center gap-1">
          {drifts.map(([svc, risk]) => (
            <span key={svc} title={`p95 drifting away from baseline (risk ${risk.toFixed(2)})`}
              className="rounded-sm border border-amber-900/60 bg-amber-950/40 px-1.5 py-0.5 font-mono text-[9px] text-amber-400">
              DRIFT {svc} {risk.toFixed(2)}
            </span>
          ))}
          {RANGES.map(([m, label]) => (
            <button key={m} onClick={() => onRange(m)}
              className={`rounded-sm px-1.5 py-0.5 font-mono text-[9px] ${range === m ? "bg-zinc-800 text-zinc-200" : "text-zinc-600 hover:text-zinc-400"}`}>
              {label}
            </button>
          ))}
        </div>
      }
    >
      {rows.length === 0 ? (
        <div className="flex h-[300px] items-center justify-center font-mono text-[11px] text-zinc-600">
          awaiting telemetry...
        </div>
      ) : (
        <>
          <div className="h-[185px] px-2 pb-1 pt-3">
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={rows} margin={{ top: 5, right: 6, left: 0, bottom: 0 }}>
                <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="ts" tick={TICK} axisLine={{ stroke: "#3f3f46" }} tickLine={false} minTickGap={48} />
                <YAxis yAxisId="ms" tick={TICK} axisLine={false} tickLine={false} width={46} tickFormatter={(v) => `${v}ms`} />
                <YAxis yAxisId="score" orientation="right" domain={[0, 1]} ticks={[0, 0.5, 1]}
                       tick={TICK} axisLine={false} tickLine={false} width={30} tickFormatter={(v) => v.toFixed(2)} />
                <Tooltip contentStyle={TOOLTIP} labelStyle={{ color: "#a1a1aa" }} cursor={{ stroke: "#3f3f46" }} />
                {services.filter((s) => selected.has(s)).map((svc) => (
                  <Line key={svc} yAxisId="ms" type="monotone" dataKey={svc} connectNulls
                        stroke={SHADES[services.indexOf(svc) % SHADES.length]} strokeWidth={1.5}
                        dot={false} isAnimationActive={false} />
                ))}
                <Line yAxisId="score" type="monotone" dataKey="anomaly" stroke="#52525b" connectNulls
                      strokeWidth={1} strokeDasharray="2 3" isAnimationActive={false} dot={anomalyDot} />
              </LineChart>
            </ResponsiveContainer>
          </div>
          <div className="h-[105px] border-t border-zinc-800/60 px-2 pb-1 pt-2">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={rows} syncId="tf-sync" margin={{ top: 2, right: 6, left: 0, bottom: 0 }}>
                <defs>
                  {services.map((svc, i) => (
                    <linearGradient key={svc} id={`fill-${i}`} x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={SHADES[i % SHADES.length]} stopOpacity={0.18} />
                      <stop offset="100%" stopColor={SHADES[i % SHADES.length]} stopOpacity={0.02} />
                    </linearGradient>
                  ))}
                </defs>
                <CartesianGrid stroke="#27272a" strokeDasharray="3 3" vertical={false} />
                <XAxis dataKey="ts" tick={TICK} axisLine={{ stroke: "#3f3f46" }} tickLine={false} minTickGap={48} />
                <YAxis tick={TICK} axisLine={false} tickLine={false} width={46} domain={[0, 100]}
                       ticks={[0, 50, 100]} tickFormatter={(v) => `${v}%`} />
                <Tooltip contentStyle={TOOLTIP} labelStyle={{ color: "#a1a1aa" }}
                         cursor={{ stroke: "#3f3f46" }}
                         formatter={(v: any) => [`${v}%`, undefined]} />
                <ReferenceLine y={30} stroke="#ef4444" strokeOpacity={0.4} strokeDasharray="4 4" />
                {services.filter((s) => selected.has(s)).map((svc) => (
                  <Area key={svc} type="monotone" dataKey={`${svc}__er`} connectNulls isAnimationActive={false}
                        stroke={SHADES[services.indexOf(svc) % SHADES.length]} strokeWidth={1.2}
                        fill={`url(#fill-${services.indexOf(svc) % SHADES.length})`} />
                ))}
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </>
      )}
      <div className="flex flex-wrap gap-1.5 border-t border-zinc-800 px-3 py-2">
        {services.map((svc) => {
          const on = selected.has(svc);
          return (
            <button key={svc} onClick={() => toggle(svc)}
              className={`flex items-center gap-1.5 rounded-sm border px-2 py-0.5 font-mono text-[10px] ${on ? "border-zinc-600 bg-zinc-800/60 text-zinc-200" : "border-zinc-800 text-zinc-500 hover:text-zinc-300"}`}>
              <span className="h-1.5 w-1.5 rounded-full"
                    style={{ background: on ? SHADES[services.indexOf(svc) % SHADES.length] : "#3f3f46" }} />
              {svc}
            </button>
          );
        })}
      </div>
    </Panel>
  );
}
