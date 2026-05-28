/**
 * File-backed sync state. The actual writer is
 * `scripts/_run_sync.py` (Python wrapper around run_pipeline.sh) —
 * Node's role is just to read this file and check whether the recorded
 * PID is still alive. Doing it this way means:
 *
 *  • Hot-reloads of the Next.js dev server don't lose state.
 *  • Multiple browser tabs see the same canonical truth.
 *  • A pipeline that crashes mid-way always writes a final exitCode
 *    (Python `try/finally`), so the dashboard never gets stuck on
 *    "running" or wrongly displays green.
 */
import { existsSync, readFileSync } from "fs";
import path from "path";

export const REPO_ROOT = path.resolve(process.cwd(), "..");
export const STATE_FILE = "/tmp/ensia_sync.state.json";

export type SyncState = {
  running: boolean;
  startedAt?: string;
  endedAt?: string;
  pid?: number;
  currentStage?: number | null;
  lastLine?: string;
  exitCode?: number | null;
  error?: string;
};

/**
 * Read the state file and reconcile it against the live process table.
 * If the file says running but the PID is gone, we return a derived
 * "ended with no exit code" state so the UI can show "failed (lost)".
 */
export function readState(): SyncState {
  if (!existsSync(STATE_FILE)) return { running: false };
  let raw: string;
  try {
    raw = readFileSync(STATE_FILE, "utf8");
  } catch {
    return { running: false };
  }
  let s: SyncState;
  try {
    s = JSON.parse(raw);
  } catch {
    return { running: false };
  }
  if (s.running && s.pid) {
    try {
      process.kill(s.pid, 0);
    } catch {
      // PID gone — pipeline died without writing its final state.
      return {
        ...s,
        running: false,
        exitCode: s.exitCode ?? -1,
        error: s.error ?? "process exited without recording an exit code",
        endedAt: s.endedAt ?? new Date().toISOString(),
      };
    }
  }
  return s;
}
