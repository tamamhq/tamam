// How a run was carried out — fast replay of a recorded script, an AI run, or both — and how
// each checklist clause was verified. Data: run.graduation + run.clauses (docs/guide/test-graduation.md).
import type { RunClause, RunGraduation, TestRun } from "@/lib/api"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { cn } from "@/lib/utils"
import { Bot, Check, CircleDashed, Code2, Eye, Info, ListChecks, Repeat, X, Zap } from "lucide-react"

type Tone = "fast" | "mixed" | "ai"

// Icon names ("chevron.forward") are not text a person reads on the screen.
const SYMBOL = /^[a-z0-9]+(?:\.[a-z0-9]+)+$/

export interface RunMode {
  label: string
  /** Colour: green = fast replay, blue = the AI agent was involved, amber = needs your attention. */
  tone: Tone
  icon: "fast" | "replay" | "ai"
  /** One sentence: what happened on this run. */
  summary: string
  /** What this kind of run IS, for the tooltip (the same for every run of this kind). */
  explain: string
}

/** Fast replay in one breath — the ⓘ next to the card title. */
const FAST_REPLAY =
  "Fast replay: once a test passes with the AI agent, Tamam compiles that run into a script. " +
  "Later runs replay the same steps and check the screen in code — seconds instead of minutes, " +
  "no AI cost — and the AI agent steps back in whenever the app has changed."

const EXPLAIN = {
  ai: "AI run: the AI agent drove the app step by step, and the evidence it cited from the screen decided the verdict.",
  fast: "Fast replay: a script recorded from an earlier passing run replayed the steps and checked the screen in code. No AI agent.",
  fastLook: "Fast replay with an AI look: the steps were replayed by the script; a few checks that code could not settle were confirmed by a model looking at the replayed screen.",
  trial: "Replay on trial: a new script replays first, then a full AI run decides the verdict. When enough runs agree, the script runs on its own.",
  handoff: "Replay → AI: the script replayed until the app no longer matched it, then the AI agent finished the test on the same device.",
  missed: "AI run (replay missed): the saved script no longer fits the app, so a full AI run decided. A passing run can be compiled into a fresh script.",
}

const TONE: Record<Tone, string> = {
  fast: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
  mixed: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
  ai: "bg-sky-500/10 text-sky-700 dark:text-sky-400",
}

function runMode(run: TestRun): RunMode | null {
  const g = run.graduation
  const r = g?.replay
  switch (g?.mode) {
    case "fast": {
      if (r?.verdict === "error")
        return { label: "Fast replay · stopped", tone: "mixed", icon: "fast", explain: EXPLAIN.fast,
                 summary: `Replay stopped before it could finish${r.reason ? `: ${r.reason}` : "."}` }
      const looks = (r?.checks ?? []).filter((c) => c.how === "ai").length
      return looks > 0
        ? { label: "Fast replay · AI look", tone: "fast", icon: "fast", explain: EXPLAIN.fastLook,
            summary: `Replayed the recorded script; ${looks} check${looks > 1 ? "s" : ""} confirmed by an AI look at the screen.` }
        : { label: "Fast replay", tone: "fast", icon: "fast", explain: EXPLAIN.fast,
            summary: "Replayed the recorded script; every check ran in code. No AI agent run." }
    }
    case "probation":
      return { label: "Replay on trial", tone: "ai", icon: "replay", explain: EXPLAIN.trial,
               summary: g?.retired
                 ? "Replayed the script, then a full AI run decided the verdict. They disagreed, so the script was retired."
                 : g?.promoted
                   ? "Replayed the script, then a full AI run decided the verdict. They agreed, so the script now runs on its own."
                   : "Replayed the script, then a full AI run decided the verdict — the script is still earning trust." }
    case "handoff":
      return { label: "Replay → AI took over", tone: "ai", icon: "replay", explain: EXPLAIN.handoff,
               summary: "Replay stopped part-way; the AI agent finished the test on the same device." }
    case "fallback":
      return { label: "AI run · replay missed", tone: "ai", icon: "ai", explain: EXPLAIN.missed,
               summary: r?.verdict === "miss"
                 ? "The script no longer fitted the app, so a full AI run decided the verdict."
                 : "Replay could not run, so a full AI run decided the verdict." }
    default:
      if (run.executor_id === "script")
        // The fast-mode record is written just after the verdict; until then, say only what we know.
        return { label: "Fast replay", tone: "fast", icon: "fast", explain: FAST_REPLAY,
                 summary: "This run used fast replay; its details are still being recorded." }
      return run.executor_id === "ios_agent"
        ? { label: "AI run", tone: "ai", icon: "ai", explain: EXPLAIN.ai, summary: "The AI agent carried out the test." }
        : null
  }
}

export function ModeBadge({ run }: { run: TestRun }) {
  const m = runMode(run)
  if (!m) return null
  const Icon = { fast: Zap, replay: Repeat, ai: Bot }[m.icon]
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <span
            tabIndex={0}
            aria-label={`${m.label}: ${m.summary}`}
            className={cn("inline-flex cursor-help items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium",
                          TONE[m.tone])}
          >
            <Icon className="size-3" aria-hidden /> {m.label}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs text-left">{m.explain}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

function InfoTip({ text }: { text: string }) {
  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger asChild>
          <button type="button" aria-label="What is fast replay?" className="text-muted-foreground hover:text-foreground">
            <Info className="size-3.5" />
          </button>
        </TooltipTrigger>
        <TooltipContent className="max-w-xs text-left font-normal">{text}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  )
}

/** What replay concluded, in plain words. */
const REPLAY_RESULT: Record<string, string> = {
  passed: "Passed",
  failed: "Found a problem",
  miss: "Didn't fit the app",
  error: "Stopped",
}

/** Where the test's script stands. */
const SCRIPT_STATUS: Record<string, string> = {
  probation: "on trial",
  active: "runs on its own",
  retired: "retired",
  superseded: "replaced",
}

function Label({ children }: { children: React.ReactNode }) {
  return <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">{children}</p>
}

/** The fast-mode story of this run: what replay did, where it stopped, and what it means for the script. */
export function FastReplayCard({ run }: { run: TestRun }) {
  const g = run.graduation
  const m = runMode(run)
  if (!g || !m) return null
  const r = g.replay
  const ran = Boolean(g.mode)
  const notPassed = Boolean(r?.verdict) && r?.verdict !== "passed"

  return (
    <Card className="gap-3">
      <CardHeader className="grid-rows-[auto] pb-0">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Zap className={cn("size-4", ran ? "text-primary" : "text-muted-foreground")} />
          {ran ? "Fast replay" : "Fast replay: not used"}
          <InfoTip text={FAST_REPLAY} />
          <ModeBadge run={run} />
          {g.script_version != null && (
            <span className="ml-auto text-xs font-normal text-muted-foreground">
              Script v{g.script_version} · {SCRIPT_STATUS[g.script_status ?? ""] ?? g.script_status}
            </span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {ran && <p className="text-foreground/80">{m.summary}</p>}

        {ran && r && (
          <div className="grid gap-3 sm:grid-cols-3">
            <div>
              <Label>Replay</Label>
              <p className="mt-0.5 font-medium">{REPLAY_RESULT[r.verdict ?? ""] ?? "—"}</p>
            </div>
            {r.seconds != null && (
              <div>
                <Label>Replay time</Label>
                <p className="mt-0.5 tabular-nums">{r.seconds}s</p>
              </div>
            )}
            {r.steps_done != null && (
              <div>
                <Label>Steps replayed</Label>
                <p className="mt-0.5 tabular-nums">{r.steps_done}</p>
              </div>
            )}
          </div>
        )}

        {ran && notPassed && r?.reason && (
          <div className="rounded-lg bg-muted/40 p-3">
            <Label>{r.verdict === "failed" ? "What replay found" : "Where replay stopped"}</Label>
            <p className="mt-1 text-foreground/80">{r.reason}</p>
            {r.seen && r.seen.length > 0 && (
              <p className="mt-1.5 text-xs text-muted-foreground">
                The screen showed instead:{" "}
                {r.seen.filter((t) => !SYMBOL.test(t)).slice(0, 8).map((t) => `“${t}”`).join(", ")}
              </p>
            )}
          </div>
        )}

        {g.mode === "handoff" && g.proven_by_replay && g.proven_by_replay.length > 0 && (
          <p className="text-xs text-muted-foreground">
            Already proven by replay before the hand-off: {g.proven_by_replay.join("; ")}
          </p>
        )}

        <ScriptOutcome g={g} leads={!ran} />
      </CardContent>
    </Card>
  )
}

/** What this run means for the test's script: promoted, retired, still on probation, or (after an AI run) compiled / refused. */
function ScriptOutcome({ g, leads = false }: { g: RunGraduation; leads?: boolean }) {
  const lines: { text: string; tone?: "good" | "bad" }[] = []
  if (g.promoted) lines.push({ text: "Promoted: future runs replay this script on their own.", tone: "good" })
  if (g.retired) lines.push({ text: `Script retired — ${g.retired}`, tone: "bad" })
  if (g.mode === "probation" && !g.promoted && !g.retired) {
    if (g.agree === false) lines.push({ text: "Replay and the AI run disagreed.", tone: "bad" })
    else if (g.agree_elsewhere)
      lines.push({ text: "Not counted toward promotion: the script started proving itself on another build or device while this run was going." })
    else if (g.agree === true && g.replay?.verdict !== "passed")
      lines.push({ text: "Replay and the AI run agreed the test fails. Only agreeing passes count toward promotion." })
    else if (g.stays_in_probation) lines.push({ text: g.stays_in_probation })
    else if (g.agree_in_a_row != null && g.promote_after != null)
      lines.push({ text: `Agreeing runs in a row: ${g.agree_in_a_row} of ${g.promote_after} needed to run on its own.` })
  }
  if (g.ready === true)
    lines.push({ text: `Ready for fast replay: this run became script v${g.version ?? "?"}, on trial until enough runs agree.`, tone: "good" })
  if (g.ready === false && g.reasons?.length)
    lines.push({ text: `Can't become a fast replay: ${g.reasons.join(" ")}` })
  if (!lines.length) return null
  return (
    <ul className={cn("space-y-1", leads ? "text-sm" : "border-t pt-3 text-xs")}>
      {lines.map((l, i) => (
        <li
          key={i}
          className={cn(
            l.tone === "good" && "text-emerald-700 dark:text-emerald-400",
            l.tone === "bad" && "text-rose-700 dark:text-rose-400",
            !l.tone && "text-muted-foreground",
          )}
        >
          {l.text}
        </li>
      ))}
    </ul>
  )
}

const HOW: Record<string, { label: string; Icon: typeof Code2; hint: string }> = {
  code: { label: "checked in code", Icon: Code2, hint: "A locked check evaluated on the replayed screen — no model involved." },
  ai: { label: "AI look", Icon: Eye, hint: "A model looked at the replayed screen and quoted the text that proves it." },
  replayed: { label: "replayed", Icon: Repeat, hint: "The recorded steps for this part were replayed." },
  agent: { label: "agent evidence", Icon: Bot, hint: "The AI agent cited the step whose screen showed it." },
}

const STATUS = {
  done: { Icon: Check, cls: "text-emerald-600 dark:text-emerald-400", sr: "done" },
  failed: { Icon: X, cls: "text-rose-600 dark:text-rose-400", sr: "failed" },
  pending: { Icon: CircleDashed, cls: "text-muted-foreground", sr: "not reached" },
}

/** "2/3 verified" — replayed actions are counted apart: a step that ran is not a step proven. */
function countLabel(clauses: RunClause[]): string {
  const done = clauses.filter((c) => c.status === "done")
  const replayed = done.filter((c) => c.how === "replayed").length
  const verified = done.length - replayed
  const checkable = clauses.length - replayed
  return replayed
    ? `${verified}/${checkable} verified · ${replayed} replayed`
    : `${verified}/${clauses.length} verified`
}

/** The run's checklist: every part of the test, its status, and how it was verified. */
export function ChecklistCard({ clauses }: { clauses: RunClause[] }) {
  if (!clauses.length) return null
  return (
    <Card className="gap-3">
      <CardHeader className="grid-rows-[auto] pb-0">
        <CardTitle className="flex items-center gap-2 text-sm">
          <ListChecks className="size-4 text-primary" /> Checklist
          <span className="text-xs font-normal text-muted-foreground">{countLabel(clauses)}</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <ol className="space-y-2.5">
          {clauses.map((c) => {
            const s = STATUS[c.status] ?? STATUS.pending
            const how = HOW[c.how ?? "agent"]
            return (
              <li key={c.id} className="flex gap-2.5 text-sm">
                <s.Icon className={cn("mt-0.5 size-4 shrink-0", s.cls)} aria-hidden />
                <span className="sr-only">{s.sr}: </span>
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                    <span className="text-foreground/90">{c.text}</span>
                    {c.status !== "pending" && (
                      <span
                        title={how.hint}
                        className="inline-flex items-center gap-1 rounded bg-muted px-1.5 py-px text-[10px] font-medium text-muted-foreground"
                      >
                        <how.Icon className="size-3" /> {how.label}
                        {c.step != null && !c.how && ` · step ${c.step}`}
                      </span>
                    )}
                  </div>
                  {c.evidence && <p className="mt-0.5 text-xs text-muted-foreground">{c.evidence}</p>}
                </div>
              </li>
            )
          })}
        </ol>
      </CardContent>
    </Card>
  )
}
