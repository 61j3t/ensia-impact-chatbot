"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { dateTime, relTime } from "@/lib/format";

// ─── stage map ────────────────────────────────────────────────────────
// run_pipeline.sh emits "▶ N/8  …" lines. We mirror those 9 stages here.

const STAGES: { num: number; key: string; label: string }[] = [
  { num: 0, key: "tg", label: "Telegram" },
  { num: 1, key: "pdf", label: "PDFs" },
  { num: 2, key: "ocr", label: "OCR" },
  { num: 3, key: "merge", label: "Merge" },
  { num: 4, key: "ensia", label: "ensia" },
  { num: 5, key: "v2v", label: "v2v" },
  { num: 6, key: "links", label: "Links" },
  { num: 7, key: "index", label: "Index" },
  { num: 8, key: "snap", label: "Snapshot" },
];

type StageState = "pending" | "active" | "done";
type Phase = "idle" | "running" | "done";

type ServerState = {
  running: boolean;
  startedAt?: string;
  endedAt?: string;
  pid?: number;
  currentStage?: number | null;
  lastLine?: string;
  exitCode?: number | null;
  error?: string;
};

// Derive a 9-element stage state array from the server's currentStage +
// running flag + final exitCode.
function deriveStages(s: ServerState): {
  stages: StageState[];
  phase: Phase;
} {
  const out: StageState[] = STAGES.map(() => "pending");
  if (s.running) {
    if (typeof s.currentStage === "number") {
      const idx = STAGES.findIndex((st) => st.num === s.currentStage);
      if (idx >= 0) {
        for (let i = 0; i < idx; i++) out[i] = "done";
        out[idx] = "active";
      }
    }
    return { stages: out, phase: "running" };
  }
  // Not running. Was there a recorded result?
  if (s.endedAt && typeof s.exitCode === "number") {
    if (s.exitCode === 0) {
      // Successful run → every stage gets the green tick.
      return {
        stages: STAGES.map(() => "done"),
        phase: "done",
      };
    }
    // Failure — show how far we got: completed stages → done, failing
    // stage → active (so the pulse highlights where it broke).
    if (typeof s.currentStage === "number") {
      const idx = STAGES.findIndex((st) => st.num === s.currentStage);
      if (idx >= 0) {
        for (let i = 0; i < idx; i++) out[i] = "done";
        out[idx] = "active";
      }
    }
    return { stages: out, phase: "done" };
  }
  // Never ran (or state-file missing) — idle.
  return { stages: out, phase: "idle" };
}

// ─── component ────────────────────────────────────────────────────────

export function DataPageTop({
  generatedAt,
  stale,
}: {
  generatedAt?: string;
  stale?: boolean;
}) {
  const router = useRouter();
  const [server, setServer] = useState<ServerState | null>(null);
  const [pollErr, setPollErr] = useState<string | null>(null);
  const wasRunningRef = useRef(false);

  // Poll status every 1.5s while the page is open. The polling rate is
  // light (one cheap file read) so we don't try to be clever about
  // backing off — keeps the UI responsive when a sync finishes.
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      try {
        const r = await fetch("/api/sync/status", { cache: "no-store" });
        const s: ServerState = await r.json();
        if (!alive) return;
        setServer(s);
        setPollErr(null);

        // If a run just transitioned running → not-running, refresh the
        // page so the server components re-read /data/_status.json,
        // feedback counts, etc.
        if (wasRunningRef.current && !s.running) {
          router.refresh();
        }
        wasRunningRef.current = s.running;
      } catch (e) {
        if (!alive) return;
        setPollErr(e instanceof Error ? e.message : String(e));
      } finally {
        if (alive) timer = setTimeout(tick, 1500);
      }
    };

    tick();
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
    };
  }, [router]);

  async function start() {
    setPollErr(null);
    // Optimistic UI: flip to running-without-stage immediately so the
    // button changes; the poll will fill in stage info ~1.5s later.
    setServer({ running: true, startedAt: new Date().toISOString() });
    try {
      const r = await fetch("/api/sync", { method: "POST" });
      if (r.status === 409) {
        // Already running from another tab — fine, polling will reflect it.
        return;
      }
      if (!r.ok) {
        const t = await r.text();
        setPollErr(`POST /api/sync ${r.status}: ${t}`);
        // Let the next poll resync.
      }
    } catch (e) {
      setPollErr(e instanceof Error ? e.message : String(e));
    }
  }

  // ── derive UI state ───────────────────────────────────────────────
  const { stages, phase } = server
    ? deriveStages(server)
    : { stages: STAGES.map(() => "pending" as StageState), phase: "idle" as Phase };

  const running = phase === "running";
  const success = phase === "done" && server?.exitCode === 0;
  const failure = phase === "done" && (server?.exitCode ?? 0) !== 0;
  const showStaleBanner = stale && !running && phase !== "done";

  return (
    <div className="space-y-4">
      <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-3 sm:gap-4">
        <div>
          <div className="kicker mb-2">/ data</div>
          <h1 className="hero-headline">
            What the bot{" "}
            <span className="italic text-coral-500">knows</span>
            <br />
            and how it&apos;s{" "}
            <span className="italic text-ocean-500">received</span>.
          </h1>
        </div>

        <div className="flex flex-col items-start sm:items-end gap-2 shrink-0">
          <button
            type="button"
            onClick={running ? undefined : start}
            disabled={running}
            className={
              "inline-flex items-center gap-2 rounded-full px-5 py-2.5 text-sm font-bold transition-all border-2 border-ink-900 shadow-brutal hover:translate-y-[1px] hover:shadow-[3px_3px_0_0_#0E1116] " +
              (running
                ? "bg-lemon-400 text-ink-900 cursor-default"
                : failure
                  ? "bg-coral-500 text-cream-50 hover:bg-coral-600"
                  : success
                    ? "bg-moss-500 text-cream-50"
                    : "bg-ink-900 text-cream-50 hover:bg-coral-500")
            }
          >
            {running ? (
              <>
                <Spinner /> Syncing…
              </>
            ) : success ? (
              <>✓ Sync done · run again</>
            ) : failure ? (
              <>↻ Retry sync</>
            ) : (
              <>↻ Sync everything</>
            )}
          </button>

          {!running && generatedAt && (
            <div className="hidden md:flex flex-col items-end gap-0.5 text-xs">
              <div className="kicker">Snapshot</div>
              <div
                title={dateTime(generatedAt)}
                className="font-display text-base font-semibold"
              >
                {relTime(generatedAt)}
              </div>
            </div>
          )}
        </div>
      </div>

      {(running || phase === "done") && (
        <StageProgress
          stages={stages}
          subLine={server?.lastLine ?? ""}
          phase={phase}
          exitCode={server?.exitCode ?? undefined}
          error={server?.error}
        />
      )}

      {showStaleBanner && (
        <div className="card-brutal bg-cream-100 px-4 py-3 text-sm">
          ⚠ Snapshot is over 24 hours old — hit{" "}
          <span className="font-semibold">Sync everything</span> to refresh.
        </div>
      )}

      {pollErr && (
        <div className="card-brutal bg-coral-50 px-4 py-3 text-sm border-coral-500/40">
          Status poll failed: <code className="font-mono">{pollErr}</code>
        </div>
      )}
    </div>
  );
}

// ─── progress bar ─────────────────────────────────────────────────────

function StageProgress({
  stages,
  subLine,
  phase,
  exitCode,
  error,
}: {
  stages: StageState[];
  subLine: string;
  phase: Phase;
  exitCode?: number;
  error?: string;
}) {
  const allDone = stages.every((s) => s === "done");
  const failed = phase === "done" && (exitCode ?? 0) !== 0;
  return (
    <section
      className={
        "card-brutal p-4 md:p-5 transition-colors " +
        (phase === "done" && allDone ? "bg-moss-50" : "")
      }
    >
      <div className="flex items-center justify-between mb-4">
        <div className="kicker">Pipeline</div>
        <div className="text-xs font-mono text-ink-900/55 truncate ml-4 max-w-[60%]">
          {phase === "done"
            ? failed
              ? `✗ failed (exit ${exitCode}${error ? ` · ${error}` : ""})`
              : allDone
                ? "✓ all stages complete"
                : ""
            : subLine || "starting…"}
        </div>
      </div>

      {/* py-2 gives the dot rings, drop-shadow, and active animate-ping halo
          vertical breathing room. `overflow-x-auto` also clips on the Y
          axis (CSS spec), so we add the room INSIDE the scroll container. */}
      <ol className="flex items-start justify-between gap-1 overflow-x-auto px-1 py-2">
        {STAGES.map((s, i) => {
          const status = stages[i];
          const prevDone = i > 0 && stages[i - 1] === "done";
          return (
            <li
              key={s.key}
              className="flex-1 min-w-[58px] flex flex-col items-center text-center"
            >
              <div className="flex items-center w-full">
                <span
                  className={
                    "h-0.5 flex-1 " +
                    (i === 0
                      ? "bg-transparent"
                      : prevDone
                        ? "bg-moss-500"
                        : "bg-ink-900/15")
                  }
                />
                <StageDot status={status} failed={failed && status === "active"} />
                <span
                  className={
                    "h-0.5 flex-1 " +
                    (i === STAGES.length - 1
                      ? "bg-transparent"
                      : status === "done"
                        ? "bg-moss-500"
                        : "bg-ink-900/15")
                  }
                />
              </div>
              <div
                className={
                  "mt-2 text-[10px] uppercase tracking-kicker font-semibold leading-tight " +
                  (status === "active"
                    ? "text-coral-600"
                    : status === "done"
                      ? "text-moss-500"
                      : "text-ink-900/40")
                }
              >
                {s.label}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function StageDot({
  status,
  failed,
}: {
  status: StageState;
  failed?: boolean;
}) {
  if (status === "done") {
    return (
      <span className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-moss-500 text-white text-[12px] font-bold ring-2 ring-ink-900 shadow-[2px_2px_0_0_#0E1116]">
        ✓
      </span>
    );
  }
  if (status === "active") {
    return (
      <span className="relative inline-flex h-6 w-6 shrink-0 items-center justify-center">
        <span
          className={
            "absolute inset-0 rounded-full animate-ping " +
            (failed ? "bg-coral-500/60" : "bg-coral-500/40")
          }
        />
        <span
          className={
            "relative h-4 w-4 rounded-full ring-2 " +
            (failed
              ? "bg-coral-500 ring-coral-600"
              : "bg-coral-500 ring-ink-900")
          }
        />
      </span>
    );
  }
  return (
    <span className="inline-flex h-6 w-6 shrink-0 rounded-full border-2 border-ink-900/15 bg-cream-50" />
  );
}

function Spinner() {
  return (
    <span
      className="inline-block h-3.5 w-3.5 rounded-full border-2 border-ink-900 border-r-transparent animate-spin"
      aria-hidden
    />
  );
}
