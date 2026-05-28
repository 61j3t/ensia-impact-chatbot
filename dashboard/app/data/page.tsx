import { loadDataStatus } from "@/lib/data-status";
import {
  getFeedbackAggregate,
  getRecentReports,
  getMostDislikedAnswers,
} from "@/lib/queries";
import { StatCard } from "@/components/StatCard";
import { FreshnessBadge } from "@/components/FreshnessBadge";
import { DataPageTop } from "@/components/DataPageTop";
import { dateTime, relTime, userDisplayName } from "@/lib/format";

export const dynamic = "force-dynamic";
export const metadata = { title: "Data · ENSIA Bot" };

// ─── formatters ────────────────────────────────────────────────────────

const fmt = (n: number) => n.toLocaleString();
const chars = (n: number) =>
  n >= 1_000_000
    ? `${(n / 1_000_000).toFixed(2)}M`
    : n >= 1_000
      ? `${(n / 1_000).toFixed(1)}k`
      : String(n);
const pct = (a: number, b: number) => (b > 0 ? Math.round((a / b) * 100) : 0);

// ─── page ──────────────────────────────────────────────────────────────

export default async function DataStatusPage() {
  const [snap, fbAgg, reports, disliked] = await Promise.all([
    loadDataStatus(),
    getFeedbackAggregate(),
    getRecentReports(8),
    getMostDislikedAnswers(5),
  ]);

  if (!snap) {
    return (
      <div className="space-y-6">
        <DataPageTop />
        <div className="card-brutal p-6 text-sm">
          No snapshot found at <code className="font-mono">data/_status.json</code>.
          Generate it:
          <pre className="mt-3 rounded-2xl bg-cream-100 border border-ink-900 p-3 text-xs">
            .venv/bin/python scripts/07_status_snapshot.py
          </pre>
        </div>
      </div>
    );
  }

  const { chat, pdfs, web, index } = snap;
  const totalPdfs = pdfs.native.files + pdfs.ocr.files;
  const totalWebPages =
    web.ensia_edu_dz.pages + web.v2v_ensia.pages + web.chat_links.total_urls;
  const usefulPct = pct(fbAgg.useful, fbAgg.total_rated);

  const snapAgeDays =
    (Date.now() - new Date(snap.generated_at).getTime()) /
    (1000 * 60 * 60 * 24);
  const snapStale = snapAgeDays > 1;

  return (
    <div className="space-y-8">
      <DataPageTop generatedAt={snap.generated_at} stale={snapStale} />

      {/* ─── KPI strip ─── */}
      <section className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <StatCard
          accent="ink"
          label="Chat messages"
          value={fmt(chat.content_messages)}
          hint={`of ${fmt(chat.total_messages)} total`}
        />
        <StatCard
          accent="ember"
          label="PDFs"
          value={totalPdfs}
          hint={`${pdfs.native.files} native · ${pdfs.ocr.files} OCR`}
        />
        <StatCard
          accent="ocean"
          label="Web pages"
          value={fmt(totalWebPages)}
          hint={`${web.ensia_edu_dz.pages} ensia · ${web.chat_links.total_urls} links`}
        />
        <StatCard
          accent="lemon"
          label="Index chunks"
          value={index.total_chunks ? fmt(index.total_chunks) : "—"}
          hint={
            index.by_source
              ? `${index.by_source.chat || 0} chat · ${
                  index.by_source.pdf || 0
                } pdf · ${index.by_source.external || 0} web`
              : "no index"
          }
        />
        <StatCard
          accent="coral"
          label="Useful rating"
          value={fbAgg.total_rated > 0 ? `${usefulPct}%` : "—"}
          hint={
            fbAgg.total_rated > 0
              ? `${fbAgg.total_rated} ratings`
              : "no feedback yet"
          }
        />
      </section>

      {/* ─── Feedback hero ─── */}
      <FeedbackSection
        fbAgg={fbAgg}
        reports={reports}
        disliked={disliked}
      />

      {/* ─── Unified corpus panel ─── */}
      <CorpusPanel snap={snap} />

      {/* ─── Topic distribution + retrieval index, side-by-side ─── */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
        <TopicsPanel chat={chat} />
        <IndexCard index={index} />
      </div>

      <ChatLinksCard web={web} />
    </div>
  );
}


// ─── feedback section ─────────────────────────────────────────────────

function FeedbackSection({
  fbAgg,
  reports,
  disliked,
}: {
  fbAgg: Awaited<ReturnType<typeof getFeedbackAggregate>>;
  reports: Awaited<ReturnType<typeof getRecentReports>>;
  disliked: Awaited<ReturnType<typeof getMostDislikedAnswers>>;
}) {
  const total = fbAgg.total_rated;
  return (
    <section className="card-brutal overflow-hidden">
      <header className="flex items-baseline justify-between px-6 py-4 border-b-2 border-ink-900 bg-cream-100">
        <h2 className="font-display text-2xl font-semibold">
          User feedback
        </h2>
        <span className="kicker">
          {total === 0 ? "no ratings yet" : `${total} ratings`}
        </span>
      </header>

      {total === 0 ? (
        <div className="px-6 py-12 text-sm text-ink-900/55 text-center">
          Buttons appear under every bot answer in Telegram
          (<span className="font-display italic">👍 · 👎 · 🚩</span>). Tap one
          and it&apos;ll appear here.
        </div>
      ) : (
        <>
          <div className="grid grid-cols-3 divide-x-2 divide-ink-900">
            <RatingCell
              emoji="👍"
              label="Useful"
              count={fbAgg.useful}
              total={total}
              tone="moss"
            />
            <RatingCell
              emoji="👎"
              label="Not useful"
              count={fbAgg.not_useful}
              total={total}
              tone="lemon"
            />
            <RatingCell
              emoji="🚩"
              label="Reports"
              count={fbAgg.report}
              total={total}
              tone="coral"
              footnote={`${fbAgg.reports_with_reason} with reason`}
            />
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 divide-y-2 md:divide-y-0 md:divide-x-2 divide-ink-900 border-t-2 border-ink-900">
            <DislikedList items={disliked} />
            <ReportsList items={reports} />
          </div>
        </>
      )}
    </section>
  );
}

function RatingCell({
  emoji,
  label,
  count,
  total,
  tone,
  footnote,
}: {
  emoji: string;
  label: string;
  count: number;
  total: number;
  tone: "moss" | "lemon" | "coral";
  footnote?: string;
}) {
  const p = pct(count, total);
  const palette = {
    moss: { bg: "bg-moss-50", text: "text-moss-500", bar: "bg-moss-500" },
    lemon: { bg: "bg-cream-100", text: "text-ink-900", bar: "bg-lemon-500" },
    coral: { bg: "bg-coral-50", text: "text-coral-600", bar: "bg-coral-500" },
  }[tone];

  return (
    <div className={`px-3 sm:px-6 py-4 sm:py-5 ${palette.bg}`}>
      <div className="flex items-baseline justify-between gap-1">
        <div className="kicker text-ink-900/70 text-[10px] sm:text-[11px] truncate">
          <span className="sm:hidden">{emoji}</span>
          <span className="hidden sm:inline">{emoji} {label}</span>
        </div>
        <div className="text-[10px] sm:text-xs font-mono text-ink-900/55 tabular-nums shrink-0">
          {p}%
        </div>
      </div>
      <div className={`numeral text-3xl sm:text-5xl md:text-6xl mt-1 sm:mt-2 ${palette.text}`}>
        {count}
      </div>
      <div className="sm:hidden mt-0.5 text-[10px] uppercase tracking-kicker text-ink-900/55 font-semibold truncate">
        {label}
      </div>
      <div className="mt-3 h-2 w-full rounded-full bg-ink-900/10 overflow-hidden">
        <div
          className={`h-full ${palette.bar} transition-all`}
          style={{ width: `${p}%` }}
        />
      </div>
      {footnote && (
        <div className="mt-2 text-xs text-ink-900/55">{footnote}</div>
      )}
    </div>
  );
}

function DislikedList({
  items,
}: {
  items: Awaited<ReturnType<typeof getMostDislikedAnswers>>;
}) {
  return (
    <div className="p-6">
      <h3 className="kicker mb-3">Most-disliked answers</h3>
      {items.length === 0 ? (
        <p className="text-sm text-ink-900/55 italic">No 👎 yet — nice.</p>
      ) : (
        <ul className="space-y-3 text-sm">
          {items.map((d, i) => (
            <li key={i} className="flex items-start gap-3">
              <span className="chip bg-cream-100 text-ink-900 ring-lemon-500/60 tabular-nums shrink-0 mt-0.5">
                👎 {d.not_useful}
              </span>
              <p className="text-ink-900/85 leading-snug">{d.content}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ReportsList({
  items,
}: {
  items: Awaited<ReturnType<typeof getRecentReports>>;
}) {
  return (
    <div className="p-6 bg-cream-50/60">
      <h3 className="kicker mb-3">Recent reports</h3>
      {items.length === 0 ? (
        <p className="text-sm text-ink-900/55 italic">No reports yet.</p>
      ) : (
        <ul className="space-y-4 text-sm">
          {items.map((r) => (
            <li key={r.feedback_id}>
              <ReportCard report={r} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ReportCard({
  report: r,
}: {
  report: Awaited<ReturnType<typeof getRecentReports>>[number];
}) {
  const who = userDisplayName({
    user_id: r.user_id,
    username: r.username,
    first_name: r.first_name,
  });
  const replyText = (r.assistant_content || "").trim();

  return (
    <div className="rounded-2.5xl border-2 border-coral-500/70 bg-white p-4 shadow-soft">
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-semibold text-ink-900">{who}</span>
        <span
          title={dateTime(r.created_at)}
          className="text-xs text-ink-900/55 shrink-0 font-mono"
        >
          {relTime(r.created_at)}
        </span>
      </div>

      <div className="mt-3">
        <div className="kicker mb-1">Reason</div>
        {r.reason ? (
          <p className="text-ink-900 whitespace-pre-wrap leading-snug">
            {r.reason}
          </p>
        ) : (
          <p className="italic text-ink-900/40">no reason given</p>
        )}
      </div>

      {replyText && (
        <div className="mt-4 pt-3 border-t border-ink-900/10">
          <div className="kicker mb-1">Reply that was reported</div>
          {/* Always rendered in full — earlier versions had a Show more
              toggle here, but expanding the card mid-grid caused the
              browser to scroll. A taller card is fine; the section has
              its own internal spacing. */}
          <p className="text-ink-900/75 leading-snug whitespace-pre-wrap">
            {replyText}
          </p>
        </div>
      )}
    </div>
  );
}

// ─── corpus / sources / index cards ───────────────────────────────────

function PanelCard({
  title,
  hint,
  children,
}: {
  title: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <section className="card-brutal overflow-hidden">
      <header className="flex items-baseline justify-between px-5 py-3 border-b-2 border-ink-900 bg-cream-100">
        <h2 className="font-display text-xl font-semibold">{title}</h2>
        {hint && <span className="kicker">{hint}</span>}
      </header>
      {children}
    </section>
  );
}

/**
 * Unified corpus panel — replaces the old Chat corpus + External sources
 * + Freshness stack. One headline number, a proportional stacked bar to
 * show "what the corpus is made of," and a per-source row table with
 * count, char volume, and freshness badge inline.
 */
function CorpusPanel({
  snap,
}: {
  snap: NonNullable<Awaited<ReturnType<typeof loadDataStatus>>>;
}) {
  const { chat, pdfs, web, index } = snap;

  type Row = {
    key: string;
    name: string;
    color: string;
    count: number;
    countLabel: string;
    chars: number;
    ts: string | null;
  };

  const rows: Row[] = [
    {
      key: "chat",
      name: "Chat messages",
      color: "bg-ink-900",
      count: chat.content_messages,
      countLabel: `${fmt(chat.content_messages)} msgs`,
      // We don't store per-chat char totals in the snapshot — leave 0
      // (we still show the chars bar weighted by other sources, and the
      // row just hides its chars cell when 0).
      chars: 0,
      ts: chat.source_file_mtime,
    },
    {
      key: "pdfs",
      name: "PDFs",
      color: "bg-ember-500",
      count: pdfs.native.files + pdfs.ocr.files,
      countLabel: `${pdfs.native.files + pdfs.ocr.files} files · ${pdfs.ocr.photos_ocr_count} photo OCR`,
      chars: pdfs.native.total_chars + pdfs.ocr.total_chars,
      ts: pdfs.native.summary_mtime,
    },
    {
      key: "ensia",
      name: "ensia.edu.dz",
      color: "bg-ocean-500",
      count: web.ensia_edu_dz.pages,
      countLabel: `${web.ensia_edu_dz.pages} pages · ${Object.entries(
        web.ensia_edu_dz.by_language,
      )
        .map(([k, v]) => `${v} ${k.toUpperCase()}`)
        .join(" · ")}`,
      chars: web.ensia_edu_dz.total_chars,
      ts: web.ensia_edu_dz.summary_mtime,
    },
    {
      key: "v2v",
      name: "v2v.ensia.edu.dz",
      color: "bg-moss-500",
      count: web.v2v_ensia.pages,
      countLabel: `${web.v2v_ensia.pages} page`,
      chars: web.v2v_ensia.total_chars,
      ts: web.v2v_ensia.summary_mtime,
    },
    {
      key: "links",
      name: "Shared links",
      color: "bg-coral-500",
      count: web.chat_links.total_urls,
      countLabel: `${web.chat_links.total_urls} pages · ${web.chat_links.total_hosts} hosts`,
      chars: web.chat_links.total_chars,
      ts: web.chat_links.manifest_mtime,
    },
  ];

  const totalChars = rows.reduce((s, r) => s + r.chars, 0);
  const totalItems =
    chat.content_messages +
    pdfs.native.files +
    pdfs.ocr.files +
    web.ensia_edu_dz.pages +
    web.v2v_ensia.pages +
    web.chat_links.total_urls;

  // Index freshness sits outside the per-source rows — surface it inline
  // beside the bar so it doesn't get lost.
  return (
    <PanelCard
      title="Corpus"
      hint={`${fmt(totalItems)} items · ${chars(totalChars)} chars`}
    >
      {/* Stacked proportional bar showing chars per source. */}
      <div className="px-5 pt-5">
        <div className="kicker mb-2">By volume (chars)</div>
        <div className="flex h-3 w-full rounded-full overflow-hidden border-2 border-ink-900">
          {rows
            .filter((r) => r.chars > 0)
            .map((r) => (
              <div
                key={r.key}
                title={`${r.name}: ${chars(r.chars)} (${Math.round(
                  (r.chars / totalChars) * 100,
                )}%)`}
                className={`h-full ${r.color}`}
                style={{ width: `${(r.chars / totalChars) * 100}%` }}
              />
            ))}
        </div>
        <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-900/55">
          {rows
            .filter((r) => r.chars > 0)
            .map((r) => (
              <span key={r.key} className="inline-flex items-center gap-1.5">
                <span className={`h-2 w-2 rounded-full ${r.color}`} />
                {r.name} · {chars(r.chars)}
              </span>
            ))}
        </div>
      </div>

      <div className="mt-5 border-t-2 border-ink-900 overflow-x-auto">
        <table className="w-full text-sm min-w-[480px]">
          <thead className="bg-cream-100 text-ink-900/55 text-xs uppercase tracking-kicker">
            <tr className="text-left border-b-2 border-ink-900">
              <th className="font-semibold px-3 sm:px-5 py-2.5">Source</th>
              <th className="font-semibold px-3 sm:px-5 py-2.5 hidden sm:table-cell">
                Volume
              </th>
              <th className="font-semibold px-3 sm:px-5 py-2.5 text-right">
                Items
              </th>
              <th className="font-semibold px-3 sm:px-5 py-2.5 text-right">
                Fresh
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-900/5">
            {rows.map((r) => (
              <tr key={r.key} className="hover:bg-cream-50">
                <td className="px-3 sm:px-5 py-3">
                  <div className="flex items-center gap-2.5">
                    <span className={`h-2.5 w-2.5 rounded-full ${r.color}`} />
                    <span className="font-semibold text-ink-900">{r.name}</span>
                  </div>
                  <div className="text-xs text-ink-900/55 mt-0.5 pl-5">
                    {r.countLabel}
                  </div>
                  {/* On phones we collapse "Volume" into this cell. */}
                  <div className="sm:hidden text-xs text-ink-900/55 mt-0.5 pl-5 tabular-nums">
                    {r.chars > 0 ? `${chars(r.chars)} chars` : ""}
                  </div>
                </td>
                <td className="px-3 sm:px-5 py-3 text-ink-900/75 tabular-nums hidden sm:table-cell">
                  {r.chars > 0 ? `${chars(r.chars)} chars` : "—"}
                </td>
                <td className="px-3 sm:px-5 py-3 text-right numeral text-base sm:text-lg">
                  {fmt(r.count)}
                </td>
                <td className="px-3 sm:px-5 py-3 text-right">
                  <FreshnessBadge ts={r.ts} />
                </td>
              </tr>
            ))}
            <tr className="bg-cream-50/60">
              <td className="px-3 sm:px-5 py-3">
                <div className="flex items-center gap-2.5">
                  <span className="h-2.5 w-2.5 rounded-full bg-lemon-500" />
                  <span className="font-semibold text-ink-900">
                    Retrieval index
                  </span>
                </div>
                <div className="text-xs text-ink-900/55 mt-0.5 pl-5">
                  BGE-M3 embeddings · ChromaDB
                </div>
                <div className="sm:hidden text-xs text-ink-900/55 mt-0.5 pl-5 tabular-nums">
                  {index.total_chunks ? `${fmt(index.total_chunks)} chunks` : ""}
                </div>
              </td>
              <td className="px-3 sm:px-5 py-3 text-ink-900/75 tabular-nums hidden sm:table-cell">
                {index.total_chunks ? `${fmt(index.total_chunks)} chunks` : "—"}
              </td>
              <td className="px-3 sm:px-5 py-3 text-right numeral text-base sm:text-lg">
                {index.total_chunks ? fmt(index.total_chunks) : "—"}
              </td>
              <td className="px-3 sm:px-5 py-3 text-right">
                <FreshnessBadge ts={index.index_mtime ?? null} />
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </PanelCard>
  );
}

/**
 * Topic distribution — top 8 forum topics as horizontal bars sized
 * relative to the largest one. Much easier to read at a glance than
 * the old "name + number" two-column list.
 */
function TopicsPanel({
  chat,
}: {
  chat: NonNullable<Awaited<ReturnType<typeof loadDataStatus>>>["chat"];
}) {
  const rows = chat.topics_top.slice(0, 8);
  const max = rows.length > 0 ? Math.max(...rows.map(([, c]) => c)) : 1;
  return (
    <PanelCard
      title="Topic distribution"
      hint={`${chat.topics_total} topics`}
    >
      <ul className="p-5 space-y-2.5 text-sm">
        {rows.map(([name, count], i) => (
          <li key={name}>
            <div className="flex justify-between items-baseline mb-1">
              <span className="text-ink-900 font-medium truncate">{name}</span>
              <span className="numeral text-base text-ink-900/70 shrink-0 ml-3">
                {count}
              </span>
            </div>
            <div className="h-2 w-full rounded-full bg-ink-900/10 overflow-hidden">
              <div
                className={
                  "h-full rounded-full " +
                  TOPIC_PALETTE[i % TOPIC_PALETTE.length]
                }
                style={{ width: `${(count / max) * 100}%` }}
              />
            </div>
          </li>
        ))}
      </ul>
    </PanelCard>
  );
}

const TOPIC_PALETTE = [
  "bg-coral-500",
  "bg-ocean-500",
  "bg-moss-500",
  "bg-lemon-500",
  "bg-ember-500",
  "bg-ink-900",
  "bg-coral-400",
  "bg-ocean-400",
];

function IndexCard({
  index,
}: {
  index: NonNullable<Awaited<ReturnType<typeof loadDataStatus>>>["index"];
}) {
  const sourceColor: Record<string, string> = {
    chat: "bg-ink-900",
    pdf: "bg-ember-500",
    external: "bg-ocean-500",
  };
  const sources = Object.entries(index.by_source || {});
  const totalChunks = sources.reduce((s, [, n]) => s + n, 0) || 1;
  const enrichedPct = pct(
    index.enriched_chat_chunks || 0,
    index.chat_chunks_total || 1,
  );

  return (
    <PanelCard title="Retrieval index" hint="BGE-M3 · cosine">
      {!index.built ? (
        <div className="p-5 text-sm text-ink-900/55">Index not built yet.</div>
      ) : index.error ? (
        <div className="p-5 text-sm text-coral-600">Error: {index.error}</div>
      ) : (
        <div className="p-5 space-y-5">
          {/* Total + stacked source bar */}
          <div>
            <div className="flex items-baseline justify-between mb-2">
              <div className="kicker">Total chunks</div>
              <div className="numeral text-3xl">
                {fmt(index.total_chunks || 0)}
              </div>
            </div>
            <div className="flex h-3 w-full rounded-full overflow-hidden border-2 border-ink-900">
              {sources.map(([k, v]) => (
                <div
                  key={k}
                  title={`${k}: ${fmt(v)} (${Math.round(
                    (v / totalChunks) * 100,
                  )}%)`}
                  className={`h-full ${sourceColor[k] || "bg-ink-900"}`}
                  style={{ width: `${(v / totalChunks) * 100}%` }}
                />
              ))}
            </div>
            <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-900/55">
              {sources.map(([k, v]) => (
                <span key={k} className="inline-flex items-center gap-1.5">
                  <span
                    className={`h-2 w-2 rounded-full ${sourceColor[k] || "bg-ink-900"}`}
                  />
                  {k} · {fmt(v)}
                </span>
              ))}
            </div>
          </div>

          {/* Enrichment progress */}
          <div className="border-t border-ink-900/10 pt-4">
            <div className="flex items-baseline justify-between mb-2">
              <div className="kicker">Link-enriched chat chunks</div>
              <div className="text-xs text-ink-900/55 font-mono tabular-nums">
                {enrichedPct}%
              </div>
            </div>
            <div className="numeral text-2xl text-ink-900">
              {fmt(index.enriched_chat_chunks || 0)}
              <span className="font-sans font-normal text-base text-ink-900/55">
                {" "}/ {fmt(index.chat_chunks_total || 0)}
              </span>
            </div>
            <div className="mt-2 h-2 w-full rounded-full bg-ink-900/10 overflow-hidden">
              <div
                className="h-full bg-moss-500"
                style={{ width: `${enrichedPct}%` }}
              />
            </div>
          </div>
        </div>
      )}
    </PanelCard>
  );
}

function ChatLinksCard({
  web,
}: {
  web: NonNullable<Awaited<ReturnType<typeof loadDataStatus>>>["web"];
}) {
  return (
    <PanelCard
      title="Shared links · per host"
      hint={`${web.chat_links.total_hosts} hosts · ${web.chat_links.total_urls} pages`}
    >
      <div className="max-h-[26rem] overflow-auto">
        <table className="w-full text-sm min-w-[480px]">
          <thead className="bg-cream-100 text-ink-900/55 sticky top-0">
            <tr className="text-xs uppercase tracking-kicker">
              <th className="text-left px-3 sm:px-5 py-3 font-semibold">Host</th>
              <th className="text-right px-3 sm:px-5 py-3 font-semibold">
                Pages
              </th>
              <th className="text-right px-3 sm:px-5 py-3 font-semibold hidden sm:table-cell">
                Chars
              </th>
              <th className="text-left px-3 sm:px-5 py-3 font-semibold hidden md:table-cell">
                Backend
              </th>
              <th className="text-right px-3 sm:px-5 py-3 font-semibold">
                Fetched
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-ink-900/5">
            {web.chat_links.hosts.map((h) => (
              <tr key={h.host} className="hover:bg-cream-50">
                <td className="px-3 sm:px-5 py-2.5 font-mono text-xs text-ink-900 break-all">
                  {h.host}
                </td>
                <td className="px-3 sm:px-5 py-2.5 text-right tabular-nums">
                  {h.pages}
                </td>
                <td className="px-3 sm:px-5 py-2.5 text-right tabular-nums text-ink-900/55 hidden sm:table-cell">
                  {fmt(h.chars)}
                </td>
                <td className="px-3 sm:px-5 py-2.5 text-ink-900/55 hidden md:table-cell">
                  {h.backend || "—"}
                </td>
                <td className="px-3 sm:px-5 py-2.5 text-right">
                  <FreshnessBadge ts={h.last_fetched} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </PanelCard>
  );
}

