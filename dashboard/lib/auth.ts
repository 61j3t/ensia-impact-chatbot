/**
 * Single-password admin auth for the dashboard.
 *
 *   ADMIN_PASSWORD   the password the user types on /login
 *   AUTH_SECRET      any random string ≥32 chars — signs the session cookie
 *
 * We don't keep a server-side session store. The cookie is its own proof:
 *
 *   value = "<expiry_unix>.<hex hmac-sha256(expiry_unix, AUTH_SECRET)>"
 *
 * On every request the middleware re-derives the HMAC and compares. If the
 * cookie is missing, malformed, tampered, or past its expiry we 302 to
 * /login. Cookie is httpOnly + Secure + SameSite=Lax + 7-day TTL.
 *
 * Web Crypto subtle is used so this module runs in Vercel's Edge runtime
 * (the middleware can't use Node's crypto).
 */

export const SESSION_COOKIE = "ensia_admin";
export const SESSION_TTL_SECONDS = 7 * 24 * 60 * 60; // 7 days

const enc = new TextEncoder();

async function _hmac(secret: string, payload: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(payload));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/** Produce a fresh cookie value valid for SESSION_TTL_SECONDS from now. */
export async function mintCookie(secret: string): Promise<string> {
  const expiry = Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS;
  const sig = await _hmac(secret, String(expiry));
  return `${expiry}.${sig}`;
}

/** Verify a cookie value. Returns true iff HMAC matches and expiry > now. */
export async function verifyCookie(
  value: string | undefined,
  secret: string,
): Promise<boolean> {
  if (!value) return false;
  const dot = value.indexOf(".");
  if (dot <= 0) return false;
  const expStr = value.slice(0, dot);
  const sig = value.slice(dot + 1);
  const exp = parseInt(expStr, 10);
  if (!Number.isFinite(exp) || exp <= Math.floor(Date.now() / 1000)) {
    return false;
  }
  const expected = await _hmac(secret, expStr);
  return _timingSafeEqual(sig, expected);
}

/** Constant-time string compare so cookie verification isn't a timing oracle. */
function _timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
