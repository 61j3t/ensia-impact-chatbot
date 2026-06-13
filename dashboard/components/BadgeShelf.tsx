import { BADGES, BADGES_BY_KEY } from "@/lib/badges";

/**
 * Renders the 12 possible badges, showing earned ones in full color
 * and locked ones desaturated. A "X / 12" header gives the user (or
 * an admin reviewing) an at-a-glance progress signal.
 */
export function BadgeShelf({
  earned,
  currentStreak,
  bestStreak,
}: {
  earned: string[] | null | undefined;
  currentStreak?: number;
  bestStreak?: number;
}) {
  const earnedSet = new Set(earned || []);
  const total = BADGES.length;
  const earnedCount = BADGES.filter((b) => earnedSet.has(b.key)).length;

  return (
    <div className="card-brutal p-4">
      <div className="flex items-baseline justify-between mb-3">
        <h3 className="kicker">Badges</h3>
        <div className="font-mono text-xs text-ink-900/55">
          {earnedCount} / {total}
        </div>
      </div>

      {(currentStreak ?? 0) > 0 && (
        <div className="mb-3 flex items-center gap-2 text-sm">
          <span className="text-lg">🔥</span>
          <span className="font-semibold">
            {currentStreak}-day streak
          </span>
          {bestStreak != null && bestStreak > (currentStreak ?? 0) && (
            <span className="text-ink-900/55">
              (best: {bestStreak})
            </span>
          )}
        </div>
      )}

      <ul className="grid grid-cols-3 sm:grid-cols-4 gap-2">
        {BADGES.map((b) => {
          const got = earnedSet.has(b.key);
          return (
            <li
              key={b.key}
              title={got ? `${b.name} — ${b.description}` : `Locked: ${b.description}`}
              className={
                "rounded-2xl border-2 px-2.5 py-2 text-center transition-colors " +
                (got
                  ? "border-ink-900 bg-cream-100 shadow-brutal"
                  : "border-ink-900/15 bg-cream-50 opacity-40 grayscale")
              }
            >
              <div className="text-2xl leading-none">{b.emoji}</div>
              <div className="mt-1 text-[10px] uppercase tracking-kicker font-semibold text-ink-900/75 leading-tight">
                {b.name}
              </div>
            </li>
          );
        })}
      </ul>

      {/* Surface any user-side badges the dashboard doesn't recognise
          (e.g. bot deployed with a newer set than the dashboard). */}
      {(earned || [])
        .filter((k) => !BADGES_BY_KEY[k])
        .map((k) => (
          <div
            key={k}
            className="mt-2 text-[10px] text-ink-900/40 font-mono"
            title="Unknown badge — bot may have a newer version"
          >
            unknown: {k}
          </div>
        ))}
    </div>
  );
}
