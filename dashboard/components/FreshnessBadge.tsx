import { relTime, dateTime } from "@/lib/format";

/**
 * Coloured badge showing the relative age of a timestamp.
 *   ≤7d → moss   8–30d → lemon   >30d → ember   null → ink dim
 *
 * Hover tooltip carries the absolute UTC timestamp.
 */
export function FreshnessBadge({ ts }: { ts: string | null | undefined }) {
  if (!ts) {
    return (
      <span className="chip bg-cream-100 text-ink-900/55 ring-ink-900/10">
        — never
      </span>
    );
  }

  const days = (Date.now() - new Date(ts).getTime()) / (1000 * 60 * 60 * 24);
  let cls = "bg-moss-50 text-moss-500 ring-moss-400/40";
  let dot = "bg-moss-500";
  if (days > 30) {
    cls = "bg-ember-50 text-ember-500 ring-ember-400/40";
    dot = "bg-ember-500";
  } else if (days > 7) {
    cls = "bg-cream-100 text-ink-900 ring-lemon-500/50";
    dot = "bg-lemon-500";
  }

  return (
    <span title={dateTime(ts)} className={`chip ${cls}`}>
      <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
      {relTime(ts)}
    </span>
  );
}
