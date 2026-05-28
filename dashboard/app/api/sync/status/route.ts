/**
 * GET /api/sync/status — read the on-disk sync state file. The state
 * file is the canonical truth (see lib/sync-state.ts) — Node verifies
 * the recorded PID is still alive and downgrades the state if not.
 */
import { NextRequest, NextResponse } from "next/server";
import { readState } from "@/lib/sync-state";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(_req: NextRequest) {
  return NextResponse.json(readState());
}
