/**
 * Small chip showing which LLM actually answered an assistant turn.
 * The bot has a fallback chain (primary → 3 fallbacks), so this lets
 * us spot at a glance whether the primary held up or we fell back.
 *
 * Color coding is per-provider, with the primary getting an "★" so it
 * stands out vs. fallbacks.
 */

function _prettyName(model: string): string {
  // Strip the provider prefix and any "-versatile" / "-instant" suffix
  // that adds noise without distinguishing the model meaningfully.
  const bare = model.replace(/^[a-z]+\//, "");
  return bare.replace(/-(versatile|instant)$/i, "");
}

function _palette(model: string) {
  if (model.startsWith("gemini/")) {
    return "bg-ocean-50 text-ocean-600 ring-ocean-400/40";
  }
  if (model.startsWith("groq/")) {
    return "bg-coral-50 text-coral-600 ring-coral-400/40";
  }
  return "bg-cream-100 text-ink-900/70 ring-ink-900/15";
}

export function ModelBadge({
  model,
  primary,
}: {
  model: string | null | undefined;
  /** The bot's configured primary model. Optional — when provided, the
   *  badge gets a leading ★ if this turn used the primary, and a
   *  leading ↪ if it fell back to a secondary. */
  primary?: string | null;
}) {
  if (!model) return null;
  const isPrimary = primary != null && model === primary;
  const marker = primary == null ? "" : isPrimary ? "★ " : "↪ ";
  return (
    <span
      title={`Answered by ${model}${
        primary && !isPrimary ? ` (fallback from ${primary})` : ""
      }`}
      className={`chip font-mono text-[10px] ${_palette(model)}`}
    >
      {marker}
      {_prettyName(model)}
    </span>
  );
}
