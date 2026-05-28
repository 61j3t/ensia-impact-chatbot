/**
 * Edge middleware: gate every dashboard route + API behind the admin
 * session cookie. Bypassed only for the login page and the login/logout
 * API endpoints themselves (otherwise nobody could ever log in).
 *
 * If AUTH_SECRET isn't set the dashboard refuses to serve anything — a
 * lazy way to make sure auth is never accidentally disabled in prod.
 */
import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE, verifyCookie } from "@/lib/auth";

// Static assets and Next internals — never gate these.
const PUBLIC_PATHS = [
  "/login",
  "/api/auth/login",
  "/api/auth/logout",
  "/_next",
  "/favicon",
];

export const config = {
  // Run the middleware on every path EXCEPT static files. Next-internal
  // `_next` paths are filtered by the matcher AND by PUBLIC_PATHS.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};

export async function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  const secret = process.env.AUTH_SECRET;
  if (!secret) {
    // Fail closed: without a secret we can't verify anything, so don't
    // serve. Surface a clear error so the misconfig is obvious in logs.
    return new NextResponse(
      "AUTH_SECRET is not set. Configure it in Vercel project settings.",
      { status: 503 },
    );
  }

  const cookie = req.cookies.get(SESSION_COOKIE)?.value;
  const ok = await verifyCookie(cookie, secret);
  if (ok) return NextResponse.next();

  // For API routes return 401 (so the client can handle it). For pages
  // redirect to /login with the original path in `?next=` so the user
  // lands back where they were after signing in.
  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const login = new URL("/login", req.url);
  if (pathname !== "/") login.searchParams.set("next", pathname);
  return NextResponse.redirect(login);
}
