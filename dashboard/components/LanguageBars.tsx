const LANG_LABELS: Record<string, string> = {
  en: "English",
  ar: "Arabic",
  fr: "French",
  es: "Spanish",
  de: "German",
  unknown: "Unknown",
};

export function LanguageBars({
  data,
}: {
  data: { language: string; count: number }[];
}) {
  if (!data.length) {
    return (
      <div className="text-sm text-zinc-500">No users yet.</div>
    );
  }
  const max = Math.max(...data.map((d) => d.count));
  return (
    <ul className="space-y-3">
      {data.map((d) => (
        <li key={d.language} className="text-sm">
          <div className="flex justify-between mb-1">
            <span className="text-zinc-700">
              {LANG_LABELS[d.language] ?? d.language}
            </span>
            <span className="tabular-nums text-zinc-500">{d.count}</span>
          </div>
          <div className="h-1.5 bg-zinc-100 rounded">
            <div
              className="h-full rounded bg-sky-500"
              style={{ width: `${(d.count / max) * 100}%` }}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}
