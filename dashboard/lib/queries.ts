/**
 * Typed read-only queries against the bot's Postgres tables.
 *
 * Schema is owned by chatbot/memory.py (Python side):
 *   users(user_id PK, username, first_name, last_name, language_code,
 *         is_bot, joined_at, last_active_at, query_count)
 *   conversations(id, chat_id, user_id, turn_idx, role, content, ts,
 *                 UNIQUE(chat_id, user_id, turn_idx))
 */
import { sql } from "./db";

export interface User {
  user_id: number;
  username: string | null;
  first_name: string | null;
  last_name: string | null;
  language_code: string | null;
  is_bot: boolean;
  joined_at: Date;
  last_active_at: Date;
  query_count: number;
}

export interface Conversation {
  chat_id: number;
  user_id: number;
  turn_idx: number;
  role: "user" | "assistant";
  content: string;
  ts: Date;
}

export interface Stats {
  totalUsers: number;
  activeUsers7d: number;
  totalMessages: number;
  totalQueries: number;
}

export async function getStats(): Promise<Stats> {
  const [row] = await sql<
    [{ total_users: number; active_7d: number; total_messages: number; total_queries: number }]
  >`
    SELECT
      (SELECT COUNT(*)::int FROM users)                                          AS total_users,
      (SELECT COUNT(*)::int FROM users
        WHERE last_active_at > NOW() - INTERVAL '7 days')                        AS active_7d,
      (SELECT COUNT(*)::int FROM conversations)                                  AS total_messages,
      (SELECT COUNT(*)::int FROM conversations WHERE role = 'user')              AS total_queries
  `;
  return {
    totalUsers: row.total_users,
    activeUsers7d: row.active_7d,
    totalMessages: row.total_messages,
    totalQueries: row.total_queries,
  };
}

export async function getUsers(): Promise<User[]> {
  return (await sql`
    SELECT user_id, username, first_name, last_name, language_code, is_bot,
           joined_at, last_active_at, query_count
    FROM users
    ORDER BY query_count DESC, last_active_at DESC
  `) as unknown as User[];
}

export async function getUser(userId: number): Promise<User | null> {
  const rows = (await sql`
    SELECT user_id, username, first_name, last_name, language_code, is_bot,
           joined_at, last_active_at, query_count
    FROM users
    WHERE user_id = ${userId}
  `) as unknown as User[];
  return rows[0] ?? null;
}

export async function getConversationsForUser(
  userId: number,
  limit = 100
): Promise<Conversation[]> {
  // Pull the most recent N rows then return them in chronological order.
  const rows = (await sql`
    SELECT chat_id, user_id, turn_idx, role, content, ts
    FROM (
      SELECT * FROM conversations
      WHERE user_id = ${userId}
      ORDER BY ts DESC
      LIMIT ${limit}
    ) recent
    ORDER BY ts ASC
  `) as unknown as Conversation[];
  return rows;
}

export async function getQueriesPerDay(
  days = 30
): Promise<{ day: string; count: number }[]> {
  const rows = (await sql`
    SELECT to_char(DATE_TRUNC('day', ts), 'YYYY-MM-DD') AS day,
           COUNT(*)::int AS count
    FROM conversations
    WHERE role = 'user' AND ts > NOW() - (${days}::int || ' days')::interval
    GROUP BY DATE_TRUNC('day', ts)
    ORDER BY day
  `) as unknown as { day: string; count: number }[];
  return rows;
}

export async function getLanguageDistribution(): Promise<
  { language: string; count: number }[]
> {
  const rows = (await sql`
    SELECT COALESCE(NULLIF(language_code, ''), 'unknown') AS language,
           COUNT(*)::int AS count
    FROM users
    WHERE is_bot = FALSE
    GROUP BY language
    ORDER BY count DESC
  `) as unknown as { language: string; count: number }[];
  return rows;
}

export async function getTopUsers(limit = 5): Promise<User[]> {
  return (await sql`
    SELECT user_id, username, first_name, last_name, language_code, is_bot,
           joined_at, last_active_at, query_count
    FROM users
    WHERE is_bot = FALSE
    ORDER BY query_count DESC
    LIMIT ${limit}
  `) as unknown as User[];
}

export async function getRecentActivity(limit = 12): Promise<
  (Conversation & { username: string | null; first_name: string | null })[]
> {
  return (await sql`
    SELECT c.chat_id, c.user_id, c.turn_idx, c.role, c.content, c.ts,
           u.username, u.first_name
    FROM conversations c
    LEFT JOIN users u ON u.user_id = c.user_id
    ORDER BY c.ts DESC
    LIMIT ${limit}
  `) as unknown as (Conversation & {
    username: string | null;
    first_name: string | null;
  })[];
}
