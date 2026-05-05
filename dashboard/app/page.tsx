import Link from "next/link";
import {
  getStats,
  getQueriesPerDay,
  getLanguageDistribution,
  getTopUsers,
  getRecentActivity,
} from "@/lib/queries";
import { StatCard } from "@/components/StatCard";
import { QueriesChart } from "@/components/QueriesChart";
import { LanguageBars } from "@/components/LanguageBars";
import { userDisplayName, relTime } from "@/lib/format";

// Don't cache — we want fresh data on every load.
export const dynamic = "force-dynamic";

export default async function OverviewPage() {
  const [stats, queriesPerDay, langs, topUsers, activity] = await Promise.all([
    getStats(),
    getQueriesPerDay(30),
    getLanguageDistribution(),
    getTopUsers(5),
    getRecentActivity(12),
  ]);

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
        <p className="text-sm text-zinc-500 mt-1">
          Live snapshot of who&apos;s using the bot and what they&apos;re asking.
        </p>
      </header>

      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <StatCard label="Total users" value={stats.totalUsers} />
        <StatCard
          label="Active (7d)"
          value={stats.activeUsers7d}
          hint="Last activity within 7 days"
        />
        <StatCard label="Queries" value={stats.totalQueries} hint="From users" />
        <StatCard
          label="Total messages"
          value={stats.totalMessages}
          hint="User + bot turns"
        />
      </section>

      <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <div className="md:col-span-2 rounded-xl border border-zinc-200 bg-white p-5">
          <h2 className="text-sm font-medium text-zinc-700 mb-4">
            Queries per day · last 30 days
          </h2>
          <QueriesChart data={queriesPerDay} />
        </div>
        <div className="rounded-xl border border-zinc-200 bg-white p-5">
          <h2 className="text-sm font-medium text-zinc-700 mb-4">
            Languages
          </h2>
          <LanguageBars data={langs} />
        </div>
      </section>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="rounded-xl border border-zinc-200 bg-white p-5">
          <h2 className="text-sm font-medium text-zinc-700 mb-4">
            Top users
          </h2>
          {topUsers.length === 0 ? (
            <p className="text-sm text-zinc-500">No users yet.</p>
          ) : (
            <ul className="space-y-3">
              {topUsers.map((u) => (
                <li
                  key={u.user_id}
                  className="flex items-center justify-between text-sm"
                >
                  <Link
                    href={`/conversations?user=${u.user_id}`}
                    className="text-zinc-900 hover:underline"
                  >
                    {userDisplayName(u)}
                  </Link>
                  <span className="text-xs text-zinc-500 tabular-nums">
                    {u.query_count} queries
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="rounded-xl border border-zinc-200 bg-white p-5">
          <h2 className="text-sm font-medium text-zinc-700 mb-4">
            Recent activity
          </h2>
          {activity.length === 0 ? (
            <p className="text-sm text-zinc-500">Nothing yet.</p>
          ) : (
            <ul className="space-y-3 text-sm">
              {activity.map((a) => (
                <li key={`${a.chat_id}-${a.user_id}-${a.turn_idx}`}>
                  <div className="flex justify-between">
                    <span
                      className={
                        a.role === "user"
                          ? "font-medium text-zinc-900"
                          : "text-zinc-600"
                      }
                    >
                      {a.role === "user"
                        ? userDisplayName(a)
                        : "bot"}
                    </span>
                    <span className="text-xs text-zinc-400">
                      {relTime(a.ts)}
                    </span>
                  </div>
                  <div className="text-zinc-600 line-clamp-2 mt-0.5">
                    {a.content}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </section>
    </div>
  );
}
