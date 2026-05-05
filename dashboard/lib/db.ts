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
    idle_timeout: 30,
    prepare: false,
  });

if (process.env.NODE_ENV !== "production") globalThis.__ensiaSql = sql;
