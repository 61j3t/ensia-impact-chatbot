"use client";

import { useState, useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/";

  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // If the user is already authenticated, bounce straight through.
  useEffect(() => {
    (async () => {
      const r = await fetch("/api/auth/check", { cache: "no-store" });
      if (r.ok) router.replace(next);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (r.ok) {
        router.replace(next);
        router.refresh();
        return;
      }
      const body = await r.json().catch(() => ({}));
      setError(body.error || "Wrong password.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="card-brutal w-full max-w-sm p-6 space-y-5"
      >
        <div>
          <div className="kicker mb-2">/ admin</div>
          <h1 className="font-display text-3xl font-semibold leading-tight">
            Sign in
          </h1>
          <p className="text-sm text-ink-900/55 mt-1">
            Enter the admin password to view the dashboard.
          </p>
        </div>

        <label className="block">
          <span className="kicker">Password</span>
          <input
            type="password"
            autoComplete="current-password"
            autoFocus
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="mt-1.5 w-full rounded-2xl border-2 border-ink-900 px-4 py-2.5 font-mono text-sm shadow-brutal focus:outline-none focus:ring-2 focus:ring-coral-500 focus:ring-offset-2 focus:ring-offset-cream-50"
            disabled={busy}
          />
        </label>

        {error && (
          <div className="rounded-2xl border-2 border-coral-500/70 bg-coral-50 px-3 py-2 text-sm text-coral-700">
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={busy || !password}
          className="w-full rounded-full bg-ink-900 text-cream-50 px-5 py-2.5 text-sm font-bold border-2 border-ink-900 shadow-brutal hover:bg-coral-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
        >
          {busy ? "Signing in…" : "Sign in →"}
        </button>
      </form>
    </div>
  );
}

export default function LoginPage() {
  // The form reads `?next=` via useSearchParams which needs Suspense.
  return (
    <Suspense fallback={null}>
      <LoginForm />
    </Suspense>
  );
}
