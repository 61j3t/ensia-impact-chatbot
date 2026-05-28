import Link from "next/link";
import {
  getStats,
  getQueriesPerDay,
  getUsers,
  getUser,
  getConversationsForUser,
} from "@/lib/queries";
import { userDisplayName, dateTime, relTime } from "@/lib/format";
import { StatCard } from "@/components/StatCard";
import { QueriesChart } from "@/components/QueriesChart";
import { SourcesList } from "@/components/SourcesList";
import { FeedbackBadges } from "@/components/FeedbackBadges";

export const dynamic = "force-dynamic";

/**
 * `/` — the only "engagement" page in the dashboard.
 *
 * Merges what used to live across /overview + /users + /conversations:
 * a strip of activity KPIs, a queries-per-day chart, then a sidebar of
 * users (sorted by activity) and the selected user's transcript with
 * feedback + citations inline.
 *
 * Pick a user via `?user=<id>`. Default = the most-active real user.
 */
export default async function HomePage({
  searchParams,
}: {
  searchParams: Promise<{ user?: string }>;
}) {
  const params = await searchParams;
  const [stats, queriesPerDay, users] = await Promise.all([
    getStats(),
    getQueriesPerDay(30),
    getUsers(),
  ]);
  const realUsers = users.filter((u) => !u.is_bot);

  const requestedId = params.user ? Number(params.user) : null;
  const selectedId =
    requestedId && realUsers.some((u) => u.user_id === requestedId)
      ? requestedId
      : realUsers[0]?.user_id ?? null;

  const [selected, turns] =
    selectedId !== null
      ? await Promise.all([
          getUser(selectedId),
          getConversationsForUser(selectedId, 200),
        ])
      : [null, []];

  return (
    <div className="space-y-8">
      <header>
        <div className="kicker mb-2">/ users</div>
        <h1 className="hero-headline">
          Everyone who&apos;s{" "}
          <span className="italic text-coral-500">talked</span> to the bot.
        </h1>
      </header>

      {/* ─── KPI strip ─── */}
      <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard
          accent="ink"
          label="Total users"
          value={stats.totalUsers}
        />
        <StatCard
          accent="moss"
          label="Active (7d)"
          value={stats.activeUsers7d}
          hint="Last activity within 7 days"
        />
        <StatCard
          accent="coral"
          label="Queries"
          value={stats.totalQueries}
          hint="User-side messages"
        />
        <StatCard
          accent="ocean"
          label="Total messages"
          value={stats.totalMessages}
          hint="User + bot turns"
        />
      </section>

      {/* ─── Queries-per-day chart ─── */}
      <section className="card-brutal overflow-hidden">
        <header className="px-5 py-3 border-b-2 border-ink-900 bg-cream-100">
          <h2 className="font-display text-xl font-semibold">
            Queries per day
            <span className="font-sans text-sm font-normal text-ink-900/55 ml-2">
              last 30 days
            </span>
          </h2>
        </header>
        <div className="p-4 sm:p-5">
          <QueriesChart data={queriesPerDay} />
        </div>
      </section>

      {/* ─── User list + transcript reader ─── */}
      <div className="grid grid-cols-1 md:grid-cols-[320px,1fr] gap-4 md:gap-5">
        <aside className="card-brutal overflow-hidden self-start md:sticky md:top-20">
          <header className="px-4 py-3 border-b-2 border-ink-900 bg-cream-100 flex items-baseline justify-between">
            <h2 className="font-display text-lg font-semibold">Users</h2>
            <span className="kicker">{realUsers.length}</span>
          </header>
          {realUsers.length === 0 ? (
            <p className="p-4 text-sm text-ink-900/55">
              No users yet — message the bot from Telegram to populate this
              list.
            </p>
          ) : (
            <ul className="divide-y divide-ink-900/5 max-h-[24rem] md:max-h-[70vh] overflow-y-auto">
              {realUsers.map((u) => {
                const active = u.user_id === selectedId;
                return (
                  <li key={u.user_id}>
                    <Link
                      href={`/?user=${u.user_id}`}
                      className={
                        "block px-4 py-3 transition-colors border-l-4 " +
                        (active
                          ? "border-coral-500 bg-coral-50"
                          : "border-transparent hover:bg-cream-50")
                      }
                    >
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="font-semibold text-ink-900 truncate">
                          {userDisplayName(u)}
                        </span>
                        <span className="numeral text-lg text-ink-900 shrink-0">
                          {u.query_count}
                        </span>
                      </div>
                      <div className="flex items-baseline justify-between gap-2 mt-0.5">
                        <span className="text-xs text-ink-900/55 truncate font-mono">
                          {u.username ? `@${u.username}` : `#${u.user_id}`}
                        </span>
                        <span className="text-[10px] text-ink-900/55 shrink-0">
                          {relTime(u.last_active_at)}
                        </span>
                      </div>
                      {u.language_code && (
                        <div className="mt-1.5">
                          <span className="chip bg-cream-100 text-ink-900/75 ring-ink-900/10 uppercase">
                            {u.language_code}
                          </span>
                        </div>
                      )}
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        <section className="card-brutal overflow-hidden">
          {selected === null ? (
            <p className="p-6 text-sm text-ink-900/55">No user selected.</p>
          ) : (
            <>
              <div className="px-5 py-4 border-b-2 border-ink-900 bg-cream-100 flex flex-wrap items-baseline justify-between gap-3">
                <div>
                  <h2 className="font-display text-xl sm:text-2xl font-semibold">
                    {userDisplayName(selected)}
                  </h2>
                  <p className="text-xs text-ink-900/55 font-mono mt-0.5 break-all">
                    user {selected.user_id}
                    {selected.username && ` · @${selected.username}`}
                  </p>
                </div>
                <div className="flex items-baseline gap-4">
                  <Metric label="Queries" value={selected.query_count} />
                  <Metric
                    label="Last active"
                    value={relTime(selected.last_active_at)}
                    title={dateTime(selected.last_active_at)}
                    small
                  />
                </div>
              </div>

              <div className="p-4 sm:p-5">
                {turns.length === 0 ? (
                  <p className="text-sm text-ink-900/55">
                    This user hasn&apos;t exchanged any messages with the bot
                    yet.
                  </p>
                ) : (
                  <ul className="space-y-4">
                    {turns.map((t) => (
                      <li
                        key={`${t.chat_id}-${t.turn_idx}`}
                        className={
                          t.role === "user"
                            ? "ml-0 mr-4 sm:mr-16"
                            : "ml-4 sm:ml-16 mr-0"
                        }
                      >
                        <div
                          className={
                            "rounded-2xl px-4 py-3 border-2 " +
                            (t.role === "user"
                              ? "border-ocean-500/40 bg-ocean-50"
                              : "border-ink-900/15 bg-cream-50")
                          }
                        >
                          <div className="flex justify-between items-baseline mb-1.5">
                            <span
                              className={
                                "text-[11px] font-semibold uppercase tracking-kicker " +
                                (t.role === "user"
                                  ? "text-ocean-600"
                                  : "text-ink-900/55")
                              }
                            >
                              {t.role}
                            </span>
                            <span className="text-xs text-ink-900/40 font-mono">
                              {dateTime(t.ts)}
                            </span>
                          </div>
                          <div className="text-sm text-ink-900 whitespace-pre-wrap leading-relaxed">
                            {t.content}
                          </div>
                          {t.role === "assistant" && (
                            <>
                              <FeedbackBadges feedback={t.feedback} />
                              <SourcesList sources={t.sources} />
                            </>
                          )}
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}
        </section>
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  small,
  title,
}: {
  label: string;
  value: string | number;
  small?: boolean;
  title?: string;
}) {
  return (
    <div className="flex flex-col items-end" title={title}>
      <span className="kicker">{label}</span>
      <span
        className={
          "numeral " + (small ? "text-base text-ink-900" : "text-2xl text-ink-900")
        }
      >
        {value}
      </span>
    </div>
  );
}
