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

export interface ConversationSource {
  id: string;
  number?: number;
  score?: number;
  preview?: string;
  metadata?: {
    source_type?: string;
    topic?: string | null;
    sender?: string | null;
    date?: string | null;
    pdf_file?: string | null;
    chunk_index?: number | null;
    site?: string | null;
    title?: string | null;
    url?: string | null;
    language?: string | null;
    [k: string]: unknown;
  };
}

export interface Conversation {
  chat_id: number;
  user_id: number;
  turn_idx: number;
  role: "user" | "assistant";
  content: string;
  ts: Date;
  sources: ConversationSource[] | null;
  tg_message_id: number | null;
  /** LiteLLM model string that actually answered the turn. NULL on
   * user turns and on rows inserted before this column existed. */
  model_used: string | null;
  feedback?: FeedbackCounts;
}

export interface FeedbackCounts {
  useful: number;
  not_useful: number;
  report: number;
}

export interface ReportRow {
  feedback_id: number;
  chat_id: number;
  message_id: number;
  user_id: number;
  reason: string | null;
  created_at: Date;
  assistant_content: string | null;
  username: string | null;
  first_name: string | null;
}

export interface FeedbackAggregate {
  total_rated: number;
  useful: number;
  not_useful: number;
  report: number;
  reports_with_reason: number;
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
  // Order by (chat_id, turn_idx) — turn_idx is the canonical sequence.
  // User + assistant rows for a single turn are inserted in the same
  // transaction so their `ts` is often identical to microsecond resolution;
  // sorting by ts alone produced apparent role-flips in the UI.
  const rows = (await sql`
    SELECT
      c.chat_id, c.user_id, c.turn_idx, c.role, c.content, c.ts,
      c.sources, c.tg_message_id, c.model_used,
      COALESCE(
        jsonb_build_object(
          'useful',     COUNT(f.id) FILTER (WHERE f.rating = 'useful'),
          'not_useful', COUNT(f.id) FILTER (WHERE f.rating = 'not_useful'),
          'report',     COUNT(f.id) FILTER (WHERE f.rating = 'report')
        ),
        '{}'::jsonb
      ) AS feedback
    FROM (
      SELECT * FROM conversations
      WHERE user_id = ${userId}
      ORDER BY turn_idx DESC
      LIMIT ${limit}
    ) c
    LEFT JOIN feedback f
      ON f.chat_id = c.chat_id AND f.message_id = c.tg_message_id
    GROUP BY c.id, c.chat_id, c.user_id, c.turn_idx, c.role, c.content,
             c.ts, c.sources, c.tg_message_id, c.model_used
    ORDER BY c.chat_id ASC, c.turn_idx ASC
  `) as unknown as Conversation[];
  return rows;
}

export async function getFeedbackAggregate(): Promise<FeedbackAggregate> {
  const [row] = (await sql`
    SELECT
      COUNT(*)::int                                          AS total_rated,
      COUNT(*) FILTER (WHERE rating = 'useful')::int         AS useful,
      COUNT(*) FILTER (WHERE rating = 'not_useful')::int     AS not_useful,
      COUNT(*) FILTER (WHERE rating = 'report')::int         AS report,
      COUNT(*) FILTER (WHERE rating = 'report' AND reason IS NOT NULL)::int
                                                             AS reports_with_reason
    FROM feedback
  `) as unknown as FeedbackAggregate[];
  return row ?? {
    total_rated: 0,
    useful: 0,
    not_useful: 0,
    report: 0,
    reports_with_reason: 0,
  };
}

export async function getRecentReports(limit = 10): Promise<ReportRow[]> {
  return (await sql`
    SELECT
      f.id AS feedback_id, f.chat_id, f.message_id, f.user_id,
      f.reason, f.created_at,
      c.content AS assistant_content,
      u.username, u.first_name
    FROM feedback f
    LEFT JOIN conversations c
      ON c.chat_id = f.chat_id AND c.tg_message_id = f.message_id
    LEFT JOIN users u ON u.user_id = f.user_id
    WHERE f.rating = 'report'
    ORDER BY f.created_at DESC
    LIMIT ${limit}
  `) as unknown as ReportRow[];
}

export async function getMostDislikedAnswers(limit = 5): Promise<
  { content: string; not_useful: number; ts: Date; user_id: number }[]
> {
  return (await sql`
    SELECT c.content,
           COUNT(f.id)::int AS not_useful,
           MIN(c.ts) AS ts,
           c.user_id
    FROM conversations c
    JOIN feedback f
      ON f.chat_id = c.chat_id
      AND f.message_id = c.tg_message_id
      AND f.rating = 'not_useful'
    WHERE c.role = 'assistant'
    GROUP BY c.id, c.content, c.user_id
    ORDER BY not_useful DESC, MIN(c.ts) DESC
    LIMIT ${limit}
  `) as unknown as {
    content: string;
    not_useful: number;
    ts: Date;
    user_id: number;
  }[];
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

