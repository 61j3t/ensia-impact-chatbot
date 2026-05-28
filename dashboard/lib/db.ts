/**
 * Postgres client for the dashboard.
 *
 * Reads `DATABASE_URL` from the environment (set in `dashboard/.env.local`,
 * which Next.js loads automatically). The dashboard talks to the same
 * Postgres instance the bot uses — by default the docker-compose service
 * published on `localhost:5433`.
 *
 * One pooled client is reused across requests via `globalThis` so dev-mode
 * hot-reloads don't open a new pool on every change.
 */
import postgres from "postgres";

const dsn = process.env.DATABASE_URL;
if (!dsn) {
  throw new Error(
    "DATABASE_URL is not set. Copy dashboard/.env.example to dashboard/.env.local " +
      "and fill in your Postgres connection string."
  );
}

declare global {
  // eslint-disable-next-line no-var
  var __ensiaSql: postgres.Sql | undefined;
}

export const sql =
  globalThis.__ensiaSql ??
  postgres(dsn, {
    max: 5,
    // `idle_timeout` triggers a Node "TimeoutNegativeWarning" in dev when
    // a connection has been idle longer than the configured value (timer
    // math goes negative; Node clamps to 1ms). The pool max already caps
    // connections, so we just let them sit.
    prepare: false,
    // postgres.js returns BIGINT as a string by default to preserve
    // precision beyond Number.MAX_SAFE_INTEGER. Telegram user_ids and
    // chat_ids are well within JS safe-int range (10–13 digits), so we
    // parse them as Number — otherwise `u.user_id === Number(params.user)`
    // comparisons in the conversations page fail silently and the page
    // falls back to the first user.
    types: {
      bigint: {
        to: 20,
        from: [20],
        serialize: (x: number | bigint) => x.toString(),
        parse: (x: string) => Number(x),
      },
    },
  });

if (process.env.NODE_ENV !== "production") globalThis.__ensiaSql = sql;
