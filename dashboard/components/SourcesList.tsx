import type { ConversationSource } from "@/lib/queries";

/**
 * Render the citations block that accompanied an assistant turn.
 * Mirrors what the Telegram bot puts under "📚 Sources" but without
 * Telegram deep-links — for the read-only dashboard we just show a
 * compact metadata label per source, sorted by citation number.
 */
function sourceLabel(s: ConversationSource): string {
  const m = s.metadata || {};
  const src = m.source_type;
  if (src === "chat") {
    const topic = m.topic || "no topic";
    const sender = m.sender || "unknown";
    const date = (m.date || "").slice(0, 10) || "?";
    return `chat · ${topic} · ${sender} · ${date}`;
  }
  if (src === "pdf") {
    return `PDF · ${m.pdf_file || "?"}${
      m.chunk_index != null ? ` · #${m.chunk_index}` : ""
    }`;
  }
  if (src === "external") {
    const site = m.site || "?";
    const title = m.title || m.url || "?";
    return `web · ${site} · ${title}`;
  }
  return s.id;
}

function sourceUrl(s: ConversationSource): string | null {
  const m = s.metadata || {};
  if (typeof m.url === "string" && m.url) return m.url;
  return null;
}

export function SourcesList({
  sources,
}: {
  sources: ConversationSource[] | null;
}) {
  if (!sources || sources.length === 0) return null;

  const ordered = [...sources].sort(
    (a, b) => (a.number ?? 999) - (b.number ?? 999)
  );

  return (
    <div className="mt-3 border-t border-zinc-200 pt-2">
      <div className="text-xs font-medium uppercase tracking-wider text-zinc-500 mb-1.5">
        📚 Sources
      </div>
      <ol className="space-y-1 text-xs">
        {ordered.map((s) => {
          const url = sourceUrl(s);
          const label = sourceLabel(s);
          return (
            <li key={s.id} className="flex gap-2">
              <span className="font-semibold text-zinc-700 tabular-nums">
                [{s.number ?? "?"}]
              </span>
              <div className="flex-1 min-w-0">
                {url ? (
                  <a
                    href={url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-sky-700 hover:underline break-all"
                  >
                    {label}
                  </a>
                ) : (
                  <span className="text-zinc-700">{label}</span>
                )}
                {s.preview && (
                  <div className="text-zinc-500 italic mt-0.5 line-clamp-2">
                    {s.preview}
                  </div>
                )}
                <div className="font-mono text-[10px] text-zinc-400 mt-0.5 break-all">
                  {s.id}
                </div>
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
