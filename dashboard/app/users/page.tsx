import Link from "next/link";
import { getUsers } from "@/lib/queries";
import { userDisplayName, dateTime, relTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function UsersPage() {
  const users = await getUsers();

  return (
    <div className="space-y-6">
      <header className="flex items-baseline justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Users</h1>
          <p className="text-sm text-zinc-500 mt-1">
            Everyone who has messaged the bot, ordered by query count.
          </p>
        </div>
        <span className="text-sm text-zinc-500">
          {users.length} {users.length === 1 ? "user" : "users"}
        </span>
      </header>

      <div className="rounded-xl border border-zinc-200 bg-white overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-zinc-50 text-zinc-600">
            <tr className="text-left">
              <th className="font-medium px-4 py-3">User</th>
              <th className="font-medium px-4 py-3">Username</th>
              <th className="font-medium px-4 py-3">Lang</th>
              <th className="font-medium px-4 py-3 text-right">Queries</th>
              <th className="font-medium px-4 py-3">Joined</th>
              <th className="font-medium px-4 py-3">Last active</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {users.length === 0 ? (
              <tr>
                <td
                  colSpan={7}
                  className="px-4 py-10 text-center text-zinc-500"
                >
                  No users yet — message the bot from Telegram to populate
                  this table.
                </td>
              </tr>
            ) : (
              users.map((u) => (
                <tr key={u.user_id} className="hover:bg-zinc-50">
                  <td className="px-4 py-3">
                    <div className="font-medium text-zinc-900">
                      {userDisplayName(u)}
                    </div>
                    <div className="text-xs text-zinc-400 font-mono">
                      {u.user_id}
                      {u.is_bot && (
                        <span className="ml-2 text-amber-600">(bot)</span>
                      )}
                    </div>
                  </td>
                  <td className="px-4 py-3 text-zinc-600">
                    {u.username ? `@${u.username}` : "—"}
                  </td>
                  <td className="px-4 py-3 text-zinc-600 uppercase text-xs">
                    {u.language_code ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-right tabular-nums">
                    {u.query_count}
                  </td>
                  <td
                    className="px-4 py-3 text-zinc-500 text-xs"
                    title={dateTime(u.joined_at)}
                  >
                    {relTime(u.joined_at)}
                  </td>
                  <td
                    className="px-4 py-3 text-zinc-500 text-xs"
                    title={dateTime(u.last_active_at)}
                  >
                    {relTime(u.last_active_at)}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Link
                      href={`/conversations?user=${u.user_id}`}
                      className="text-sky-600 hover:underline text-xs"
                    >
                      View →
                    </Link>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
