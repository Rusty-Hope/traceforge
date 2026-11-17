import type { ReactNode } from "react";

export function Panel({ title, right, children, className = "" }: {
  title: string;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`flex flex-col rounded-sm border border-zinc-800 bg-zinc-900/30 ${className}`}>
      <header className="flex h-8 shrink-0 items-center justify-between border-b border-zinc-800 px-3">
        <h2 className="font-mono text-[10px] tracking-[0.18em] text-zinc-500">{title}</h2>
        {right}
      </header>
      {children}
    </section>
  );
}

export function Section({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 font-mono text-[9px] tracking-[0.18em] text-zinc-600">{label}</div>
      {children}
    </div>
  );
}

export function KV({ k, v, accent = false }: { k: string; v: ReactNode; accent?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-3 border-b border-zinc-900 py-1 last:border-0">
      <span className="font-mono text-[10px] text-zinc-600">{k}</span>
      <span className={`text-right font-mono text-[11px] ${accent ? "text-red-400" : "text-zinc-300"}`}>{v}</span>
    </div>
  );
}

export function Badge({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-sm border border-zinc-800 bg-zinc-900 px-1.5 py-px font-mono text-[9px] text-zinc-400">
      {children}
    </span>
  );
}

const SEV_STYLES: Record<string, string> = {
  critical: "border-red-900/60 bg-red-950/40 text-red-400",
  high: "border-orange-900/60 bg-orange-950/40 text-orange-400",
  medium: "border-yellow-900/60 bg-yellow-950/40 text-yellow-500",
  low: "border-zinc-800 bg-zinc-900 text-zinc-400",
};

export function SevChip({ sev, state }: { sev: string | null; state: string }) {
  if (!sev) {
    const cls = state === "error"
      ? "border-red-900/60 bg-red-950/40 text-red-400"
      : "border-zinc-800 bg-zinc-900 text-zinc-600";
    return <span className={`inline-block rounded-sm border px-1.5 py-px font-mono text-[9px] uppercase ${cls}`}>{state}</span>;
  }
  return (
    <span className={`inline-block rounded-sm border px-1.5 py-px font-mono text-[9px] uppercase ${SEV_STYLES[sev] ?? SEV_STYLES.low}`}>
      {sev}
    </span>
  );
}
