/**
 * POST /api/sync — kick off scripts/_run_sync.py as a fully detached
 * child. The wrapper writes state to /tmp/ensia_sync.state.json — that
 * file is the source of truth, and survives Next.js hot-reloads.
 *
 * We deliberately do NOT stream output back. The dashboard polls the
 * status endpoint at /api/sync/status — that's the same code path
 * whether the user just clicked Sync, refreshed the page mid-run, or
 * opened a second tab.
 *
 * Security: localhost-only (see `isLocalhost`).
 */
import { spawn } from "child_process";
import { NextRequest, NextResponse } from "next/server";
import path from "path";

import { REPO_ROOT, readState } from "@/lib/sync-state";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function isLocalhost(req: NextRequest): boolean {
  const xff = req.headers.get("x-forwarded-for") ?? "";
  const firstHop = xff.split(",")[0]?.trim();
  if (firstHop && firstHop !== "127.0.0.1" && firstHop !== "::1") return false;
  const host = (req.headers.get("host") ?? "").split(":")[0];
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

export async function POST(req: NextRequest) {
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

  // Spawn the wrapper detached and unref it — survives dev-server
  // hot-reloads, dashboard restarts, even the user closing the tab.
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
