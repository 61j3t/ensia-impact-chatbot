/**
 * POST /api/sync — kick off the pipeline.
 *
 *   • Production (Vercel): proxies to the HF Space sidecar at
 *     `${SYNC_BACKEND_URL}/sync`. The sidecar spawns `_run_sync.py`
 *     server-side and writes state to its own filesystem.
 *
 *   • Local dev (no SYNC_BACKEND_URL set): spawns the wrapper directly
 *     as a detached child. Survives Next.js hot-reloads.
 *
 * Either path returns immediately with `{ok, pid}`. The dashboard polls
 * `/api/sync/status` to drive the stage stepper.
 *
 * Security: when running locally, localhost-only.
 */
import { spawn } from "child_process";
import { NextRequest, NextResponse } from "next/server";
import path from "path";

import { REPO_ROOT, readState } from "@/lib/sync-state";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND = process.env.SYNC_BACKEND_URL?.replace(/\/$/, "");

function isLocalhost(req: NextRequest): boolean {
  const xff = req.headers.get("x-forwarded-for") ?? "";
  const firstHop = xff.split(",")[0]?.trim();
  if (firstHop && firstHop !== "127.0.0.1" && firstHop !== "::1") return false;
  const host = (req.headers.get("host") ?? "").split(":")[0];
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

export async function POST(req: NextRequest) {
  // Production path: proxy to the HF Space sidecar.
  if (BACKEND) {
    try {
      const r = await fetch(`${BACKEND}/sync`, { method: "POST" });
      const body = await r.text();
      return new NextResponse(body, {
        status: r.status,
        headers: { "Content-Type": r.headers.get("content-type") ?? "application/json" },
      });
    } catch (e) {
      return NextResponse.json(
        { error: "sidecar unreachable", detail: String(e) },
        { status: 502 },
      );
    }
  }

  // Local dev path: spawn the wrapper directly.
  if (!isLocalhost(req)) {
    return new Response("forbidden (localhost only)", { status: 403 });
  }

  const current = readState();
  if (current.running) {
    return NextResponse.json(
      { error: "a sync is already running", state: current },
      { status: 409 },
    );
  }

  const child = spawn(
    path.join(REPO_ROOT, ".venv/bin/python"),
    ["scripts/_run_sync.py"],
    {
      cwd: REPO_ROOT,
      detached: true,
      stdio: "ignore",
      env: {
        ...process.env,
        HF_HUB_OFFLINE: "1",
        PYTHONUNBUFFERED: "1",
        PYTHONPATH: ".",
      },
    },
  );
  child.unref();

  return NextResponse.json({ ok: true, pid: child.pid });
}
