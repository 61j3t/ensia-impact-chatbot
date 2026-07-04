import { StatCard } from "./StatCard";
import type { EventRow, HealthSummary } from "@/lib/queries";
import { relTime, dateTime } from "@/lib/format";

/**
 * Operational health: 24h error/latency KPIs from the events table +
 * conversations telemetry, plus a recent-events feed. This is the
 * durable replacement for scraping HF's ephemeral stdout logs.
 */
export function HealthPanel({
  health,
  events,
}: {
  health: HealthSummary;
  events: EventRow[];
}) {
  const fmtMs = (ms: number | null) =>
    ms == null ? "—" : ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
  const geminiPct =
    health.geminiShare == null ? "—" : `${Math.round(health.geminiShare * 100)}%`;

  return (
    <section className="space-y-4">
      <header className="flex items-baseline justify-between">
        <h2 className="font-display text-xl font-semibold">System health</h2>
        <span className="kicker">last 24h</span>
      </header>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          accent={health.errors24h > 0 ? "coral" : "moss"}
          label="Errors"
          value={health.errors24h}
          hint={`${health.warns24h} warnings`}
        />
        <StatCard
          accent="ink"
          label="Answers"
          value={health.answers24h}
        />
        <StatCard
          accent={
            health.p95_ms != null && health.p95_ms > 20000 ? "ember" : "ocean"
          }
          label="Latency p50 / p95"
          value={`${fmtMs(health.p50_ms)} / ${fmtMs(health.p95_ms)}`}
          hint={`max ${fmtMs(health.max_ms)}`}
        />
        <StatCard
          accent="lemon"
          label="On primary (Gemini)"
          value={geminiPct}
          hint="rest served by fallback"
        />
      </div>

      <div className="card-brutal overflow-hidden">
        <header className="px-4 py-3 border-b-2 border-ink-900 bg-cream-100 flex items-baseline justify-between">
          <h3 className="font-display text-lg font-semibold">Recent events</h3>
          <span className="kicker">WARNING+</span>
        </header>
        {events.length === 0 ? (
          <p className="p-4 text-sm text-ink-900/55">
            No warnings or errors logged. 🎉
          </p>
        ) : (
          <ul className="divide-y divide-ink-900/5 max-h-[26rem] overflow-y-auto">
            {events.map((e) => (
              <li key={e.id} className="px-4 py-3">
                <div className="flex items-baseline justify-between gap-2">
                  <span
                    className={
                      "chip uppercase " +
                      (e.level === "ERROR" || e.level === "CRITICAL"
                        ? "bg-coral-50 text-coral-600 ring-coral-500/30"
                        : "bg-lemon-50 text-ink-900/70 ring-ink-900/10")
                    }
                  >
                    {e.level}
                  </span>
                  <span
                    className="text-[10px] text-ink-900/45 font-mono shrink-0"
                    title={dateTime(e.ts)}
                  >
                    {relTime(e.ts)}
                  </span>
                </div>
                <div className="mt-1 text-sm text-ink-900 break-words">
                  {e.message}
                </div>
                {e.logger && (
                  <div className="mt-0.5 text-[10px] text-ink-900/40 font-mono">
                    {e.logger}
                  </div>
                )}
                {e.exc_info && (
                  <details className="mt-1">
                    <summary className="text-[11px] text-ink-900/55 cursor-pointer">
                      traceback
                    </summary>
                    <pre className="mt-1 text-[10px] text-ink-900/70 bg-cream-50 border border-ink-900/10 rounded-lg p-2 overflow-x-auto whitespace-pre">
                      {e.exc_info}
                    </pre>
                  </details>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
