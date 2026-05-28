/**
 * GET /api/auth/check — quick "am I authenticated" probe used by the
 * login page on mount so authenticated users skip the form entirely.
 *
 * Returns 200 when the middleware allows the request through (which it
 * only does when the cookie verifies). Returns 401 otherwise — the
 * middleware short-circuits before this handler runs.
 */
import { NextResponse } from "next/server";

export const runtime = "edge";

export async function GET() {
  return NextResponse.json({ ok: true });
}
