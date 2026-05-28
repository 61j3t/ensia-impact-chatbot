import type { FeedbackCounts } from "@/lib/queries";

/** Compact 👍N · 👎N · 🚩N row under each assistant turn. Zero counts
 *  drop out — no need to show three "0"s for unrated replies. */
export function FeedbackBadges({
  feedback,
}: {
  feedback: FeedbackCounts | null | undefined;
}) {
  if (!feedback) return null;
  const items: { emoji: string; n: number; cls: string }[] = [];
  if (feedback.useful)
    items.push({
      emoji: "👍",
      n: feedback.useful,
      cls: "bg-moss-50 text-moss-500 ring-moss-400/40",
    });
  if (feedback.not_useful)
    items.push({
      emoji: "👎",
      n: feedback.not_useful,
      cls: "bg-cream-100 text-ink-900 ring-lemon-500/50",
    });
  if (feedback.report)
    items.push({
      emoji: "🚩",
      n: feedback.report,
      cls: "bg-coral-50 text-coral-600 ring-coral-400/40",
    });
  if (items.length === 0) return null;

  return (
    <div className="mt-2 flex gap-1.5">
      {items.map((it) => (
        <span key={it.emoji} className={`chip ${it.cls}`}>
          <span>{it.emoji}</span>
          <span className="tabular-nums">{it.n}</span>
        </span>
      ))}
    </div>
  );
}
