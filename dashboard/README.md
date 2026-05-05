# Dashboard

Read-only Next.js 15 app that displays the bot's Postgres data — users
and conversations — in three views:

- **Overview** — total/active users, queries per day, language mix,
  top users, recent activity feed
- **Users** — full user table sorted by query count
- **Conversations** — pick a user, read their full thread with the bot

Server components query Postgres directly via [postgres.js]. No API
layer, no client-side data fetching.

## Run

Postgres must be up first (it's the same DB the bot uses):

```bash
# from the repo root
docker compose up -d postgres
```

Then in this directory:

```bash
npm install
cp .env.example .env.local            # then edit DATABASE_URL if needed
npm run dev
```

→ <http://localhost:3000>

## Stack

- Next.js 15.5 (App Router, Turbopack dev server)
- React 19
- TypeScript 5.7
- Tailwind CSS 3.4
- [postgres.js](https://github.com/porsager/postgres) for DB access
- Recharts for the queries-per-day chart

## Notes

- All data is fetched server-side. No DB credentials reach the browser.
- The dashboard is unauthenticated. Don't expose it on a public URL
  without putting auth in front (a reverse proxy with basic auth, or
  Next-Auth).
- Pages disable caching (`force-dynamic`) so a refresh always reflects
  the latest Postgres state. Hit ⌘R to update.
