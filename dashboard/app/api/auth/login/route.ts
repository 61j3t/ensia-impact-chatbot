/**
 * POST /api/auth/login — validate the password, set the session cookie.
 *
 * 400 if the body is malformed, 401 if the password doesn't match,
 * 503 if the server isn't configured (missing ADMIN_PASSWORD/AUTH_SECRET).
 */
import { NextRequest, NextResponse } from "next/server";
import {
  SESSION_COOKIE,
  SESSION_TTL_SECONDS,
  mintCookie,
} from "@/lib/auth";

export const runtime = "edge";

export async function POST(req: NextRequest) {
  const admin = process.env.ADMIN_PASSWORD;
  const secret = process.env.AUTH_SECRET;
  if (!admin || !secret) {
    return NextResponse.json(
      { error: "server not configured (ADMIN_PASSWORD or AUTH_SECRET missing)" },
      { status: 503 },
    );
  }

  let body: { password?: string };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "bad request" }, { status: 400 });
  }

  // Constant-time compare on the password — same reasoning as the cookie.
  const a = body.password ?? "";
  const b = admin;
  if (a.length !== b.length) {
    return NextResponse.json({ error: "wrong password" }, { status: 401 });
  }
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  if (diff !== 0) {
    return NextResponse.json({ error: "wrong password" }, { status: 401 });
  }

  const value = await mintCookie(secret);
  const res = NextResponse.json({ ok: true });
  res.cookies.set(SESSION_COOKIE, value, {
    httpOnly: true,
    secure: true,
    sameSite: "lax",
    path: "/",
    maxAge: SESSION_TTL_SECONDS,
  });
  return res;
}
