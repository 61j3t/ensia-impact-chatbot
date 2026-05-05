import Link from "next/link";
import {
  getUsers,
  getUser,
  getConversationsForUser,
} from "@/lib/queries";
import { userDisplayName, dateTime } from "@/lib/format";

export const dynamic = "force-dynamic";

export default async function ConversationsPage({
  searchParams,
}: {
  searchParams: Promise<{ user?: string }>;
}) {
  const params = await searchParams;
  const users = await getUsers();
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
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">
          Conversations
        </h1>
        <p className="text-sm text-zinc-500 mt-1">
          Pick a user on the left to read their full back-and-forth with the bot.
        </p>
      </header>

      <div className="grid grid-cols-1 md:grid-cols-[260px,1fr] gap-4">
        <aside className="rounded-xl border border-zinc-200 bg-white">
          {realUsers.length === 0 ? (
            <p className="p-4 text-sm text-zinc-500">No users yet.</p>
          ) : (
            <ul className="divide-y divide-zinc-100">
              {realUsers.map((u) => {
                const active = u.user_id === selectedId;
                return (
                  <li key={u.user_id}>
                    <Link
                      href={`/conversations?user=${u.user_id}`}
                      className={
                        "block px-4 py-3 text-sm transition-colors " +
                        (active
                          ? "bg-sky-50 text-sky-900"
                          : "hover:bg-zinc-50")
                      }
                    >
                      <div className="font-medium">{userDisplayName(u)}</div>
                      <div className="text-xs text-zinc-500 mt-0.5">
                        {u.query_count} queries
                        {u.username && ` · @${u.username}`}
                      </div>
                    </Link>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        <section className="rounded-xl border border-zinc-200 bg-white p-5 min-h-[400px]">
          {selected === null ? (
            <p className="text-sm text-zinc-500">No user selected.</p>
          ) : (
            <>
              <div className="flex items-baseline justify-between border-b border-zinc-100 pb-3 mb-4">
                <div>
                  <h2 className="text-lg font-semibold">
                    {userDisplayName(selected)}
                  </h2>
                  <p className="text-xs text-zinc-500 font-mono mt-0.5">
                    user {selected.user_id}
                    {selected.username && ` · @${selected.username}`}
                  </p>
                </div>
                <span className="text-sm text-zinc-500">
                  {turns.length} {turns.length === 1 ? "turn" : "turns"}
                </span>
              </div>

              {turns.length === 0 ? (
                <p className="text-sm text-zinc-500">
                  This user hasn&apos;t exchanged any messages with the bot yet.
                </p>
              ) : (
                <ul className="space-y-4">
                  {turns.map((t) => (
                    <li
                      key={`${t.chat_id}-${t.turn_idx}`}
                      className={
                        t.role === "user" ? "ml-0 mr-12" : "ml-12 mr-0"
                      }
                    >
                      <div
                        className={
                          "rounded-lg px-3 py-2 " +
                          (t.role === "user"
                            ? "bg-sky-50 border border-sky-100"
                            : "bg-zinc-50 border border-zinc-200")
                        }
                      >
                        <div className="flex justify-between items-baseline mb-1">
                          <span
                            className={
                              "text-xs font-medium uppercase tracking-wide " +
                              (t.role === "user"
                                ? "text-sky-700"
                                : "text-zinc-500")
                            }
                          >
                            {t.role}
                          </span>
                          <span className="text-xs text-zinc-400">
                            {dateTime(t.ts)}
                          </span>
                        </div>
                        <div className="text-sm text-zinc-800 whitespace-pre-wrap">
                          {t.content}
                        </div>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
