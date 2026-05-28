/**
 * GET /api/sync/status — current pipeline state.
 *
 *   • Production (Vercel): forwards to the HF Space sidecar's status
 *     endpoint. The state file lives on the bot's container.
 *
 *   • Local dev: reads `/tmp/ensia_sync.state.json` directly.
 *
 * Same response shape in both cases — the dashboard's DataPageTop
 * doesn't need to know where the state came from.
 */
import { NextRequest, NextResponse } from "next/server";
import { readState } from "@/lib/sync-state";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKEND = process.env.SYNC_BACKEND_URL?.replace(/\/$/, "");
const SYNC_TOKEN = process.env.SYNC_TOKEN;

export async function GET(_req: NextRequest) {
  if (BACKEND) {
    try {
      const r = await fetch(`${BACKEND}/sync/status`, {
        cache: "no-store",
        headers: SYNC_TOKEN ? { "X-Sync-Token": SYNC_TOKEN } : {},
      });
      const body = await r.text();
      return new NextResponse(body, {
        status: r.status,
        headers: { "Content-Type": "application/json" },
      });
    } catch (e) {
      return NextResponse.json(
        { running: false, error: `sidecar unreachable: ${String(e)}` },
        { status: 200 }, // soft-fail so the UI doesn't flash an error
      );
    }
  }
  return NextResponse.json(readState());
}
