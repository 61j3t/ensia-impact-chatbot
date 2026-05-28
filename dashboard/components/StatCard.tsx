type Accent = "ink" | "coral" | "ocean" | "moss" | "lemon" | "ember";

/**
 * Bold KPI card — kicker label up top, oversized serif numeral, optional
 * hint underneath, and a small accent dot in the corner. Designed to be
 * dropped into a grid (gap-4) without extra wrappers.
 */
export function StatCard({
  label,
  value,
  hint,
  accent = "ink",
}: {
  label: string;
  value: string | number;
  hint?: string;
  accent?: Accent;
}) {
  return (
    <div className="card-brutal p-4 sm:p-5 flex flex-col gap-1.5 sm:gap-2 relative overflow-hidden">
      <span
        className={`absolute top-3 right-3 h-2.5 w-2.5 rounded-full ${ACCENT_DOT[accent]}`}
      />
      <div className="kicker text-[10px] sm:text-[11px]">{label}</div>
      <div className="numeral text-3xl sm:text-4xl md:text-5xl text-ink-900 leading-none">
        {value}
      </div>
      {hint && (
        <div className="text-[11px] sm:text-xs text-ink-900/55 leading-snug">
          {hint}
        </div>
      )}
    </div>
  );
}

const ACCENT_DOT: Record<Accent, string> = {
  ink: "bg-ink-900",
  coral: "bg-coral-500",
  ocean: "bg-ocean-500",
  moss: "bg-moss-500",
  lemon: "bg-lemon-500",
  ember: "bg-ember-500",
};
