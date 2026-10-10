import { useEffect, useRef, useState } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { useMutation, useQuery } from "@tanstack/react-query"
import { api, type TestRun, type Verdict } from "@/lib/api"
import { useRunStream } from "@/hooks/use-run-stream"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { OutcomeBadge } from "@/components/outcome-badge"
import { ChecklistCard, FastReplayCard, ModeBadge } from "@/components/fast-replay"
import { cn, parseServerTime } from "@/lib/utils"
import {
  ArrowLeft,
  Bot,
  Check,
  Film,
  FlaskConical,
  MonitorPlay,
  Radio,
  RotateCw,
  Route,
  ShieldCheck,
  Timer,
  TriangleAlert,
  X,
} from "lucide-react"

export function RunDetailPage() {
  const { runId = "" } = useParams()
  const navigate = useNavigate()

  const { data, refetch } = useQuery({
    queryKey: ["run", runId],
    queryFn: () => api.runs.get(runId),
    refetchInterval: (query) => {
      const r = query.state.data?.run
      if (!r) return 3000
      if (r.lifecycle !== "finished") return 3000
      // Finished, but the recording URL can land a few seconds later (cloud video
      // finalizing) — keep polling briefly so it appears without a manual refresh.
      // The fast-mode record (run.graduation) is also written just after the verdict.
      const pendingGrad = !r.graduation && (r.executor_id === "ios_agent" || r.executor_id === "script")
      if ((!r.video_url || pendingGrad) && r.finished_at && Date.now() - parseServerTime(r.finished_at) < 30000)
        return 4000
      return false
    },
  })

  const rerun = useMutation({
    mutationFn: (testId: string) => api.tests.run(testId),
    onSuccess: (r) => navigate(`/runs/${r.id}`),
  })

  const run = data?.run
  const { data: test } = useQuery({
    queryKey: ["test", run?.test_id],
    enabled: !!run?.test_id,
    queryFn: () => api.tests.get(run!.test_id),
  })
  const isLive = !!run && run.lifecycle !== "finished"
  const { steps: liveSteps, lifecycle: streamLifecycle, frame } = useRunStream(runId, isLive)
  const [lightbox, setLightbox] = useState<string | null>(null)

  // Live elapsed counter while running. Reset implicitly per run via startMs below.
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => {
    if (!isLive) return
    setNowMs(Date.now())
    const id = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(id)
  }, [isLive, runId])
  const startMs = run?.started_at
    ? parseServerTime(run.started_at)
    : run?.created_at
      ? parseServerTime(run.created_at)
      : nowMs
  const elapsedS = Math.max(0, Math.floor((nowMs - startMs) / 1000))
  const elapsedLabel =
    elapsedS >= 60 ? `${Math.floor(elapsedS / 60)}:${String(elapsedS % 60).padStart(2, "0")}` : `${elapsedS}s`

  // When the stream says finished, pull the final state (verdicts, usage)
  useEffect(() => {
    if (streamLifecycle === "finished") refetch()
  }, [streamLifecycle, refetch])

  const steps = isLive && liveSteps.length > 0
    ? liveSteps
    : (data?.steps ?? []).map((s) => ({
        step: s.step,
        payload: s.payload,
        screenshot_url: s.screenshot_url,
        annotated_url: s.annotated_url ?? null,
      }))

  const bottomRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!isLive) return
    // Scroll only the step list's own viewport — NOT the page (scrollIntoView would
    // bubble up and drag the live preview off-screen).
    const viewport = bottomRef.current?.closest(
      "[data-radix-scroll-area-viewport]",
    ) as HTMLElement | null
    if (viewport) viewport.scrollTop = viewport.scrollHeight
  }, [steps.length, isLive])

  if (!run) return null

  const stepList = (
    <div className="space-y-0">
      {steps.map((step, i) => {
        const action = (step.payload.actions?.[0]?.action as string) ?? "step"
        const last = i === steps.length - 1
        return (
          <div key={step.step} className="step-enter relative flex gap-3 pb-4">
            {/* timeline rail: numbered node + connector */}
            <div className="flex flex-col items-center">
              <div className="flex size-7 shrink-0 items-center justify-center rounded-full border-2 border-primary/30 bg-background text-[11px] font-semibold text-primary">
                {step.step}
              </div>
              {!last && <div className="mt-1 w-px flex-1 bg-border" />}
            </div>
            <div className="min-w-0 flex-1 rounded-lg border border-border/60 bg-background/40 p-3">
              <div className="flex items-center gap-2">
                <ActionBadge action={action} />
                {step.payload.duration_s != null && (
                  <span className="ml-auto shrink-0 text-[10px] text-muted-foreground">
                    {step.payload.duration_s}s
                  </span>
                )}
              </div>
              {step.payload.next_goal && (
                <p className="mt-2 text-sm leading-relaxed text-foreground/90">{step.payload.next_goal}</p>
              )}
              {step.screenshot_url && (
                <button
                  type="button"
                  onClick={() => setLightbox(step.screenshot_url!)}
                  className="mt-3 inline-block rounded-lg border border-border/60 bg-muted p-1.5 transition-opacity hover:opacity-90"
                  title="Click to enlarge"
                >
                  <img
                    src={step.screenshot_url}
                    alt={`step ${step.step}`}
                    className="max-h-56 w-auto rounded-md object-contain shadow-sm"
                  />
                </button>
              )}
              {step.annotated_url && (
                <button
                  type="button"
                  onClick={() => setLightbox(step.annotated_url!)}
                  className="mt-1.5 block rounded-md border border-border/60 bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                  title="The annotated set-of-marks image the agent actually saw"
                >
                  ◉ Agent view
                </button>
              )}
            </div>
          </div>
        )
      })}
      {steps.length === 0 && (
        <p className="py-10 text-center text-sm text-muted-foreground">
          {isLive ? "Waiting for the first step…" : "No steps recorded."}
        </p>
      )}
      <div ref={bottomRef} />
    </div>
  )

  const stepStreamHeader = (
    <CardHeader className="pb-2">
      <CardTitle className="flex items-center gap-2 text-sm">
        <MonitorPlay className="size-4 text-primary" /> Step stream
      </CardTitle>
    </CardHeader>
  )

  // Finished view: a filmstrip grid of the journey — screenshots get real room and
  // the whole thing flows in the page's single scroll (no nested scrollbar).
  const stepGrid = (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
      {steps.map((step) => {
        const action = (step.payload.actions?.[0]?.action as string) ?? "step"
        return (
          <div
            key={step.step}
            className="step-enter flex flex-col overflow-hidden rounded-xl border bg-card"
          >
            <div className="flex items-center gap-2 border-b px-3 py-2">
              <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-primary/10 text-[11px] font-semibold text-primary">
                {step.step}
              </span>
              <ActionBadge action={action} />
              {step.payload.actions?.[0]?.replay === true && action !== "ai_look" && (
                <span className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:text-emerald-400">
                  fast replay
                </span>
              )}
              {step.payload.duration_s != null && (
                <span className="ml-auto shrink-0 font-mono text-[10px] text-muted-foreground">
                  {step.payload.duration_s}s
                </span>
              )}
            </div>
            {step.screenshot_url && (
              <button
                type="button"
                onClick={() => setLightbox(step.screenshot_url!)}
                className="flex items-center justify-center bg-muted/40 p-3 transition-opacity hover:opacity-90"
                title="Click to enlarge"
              >
                <img
                  src={step.screenshot_url}
                  alt={`step ${step.step}`}
                  className="max-h-72 w-auto rounded-md object-contain shadow-sm"
                />
              </button>
            )}
            {step.annotated_url && (
              <button
                type="button"
                onClick={() => setLightbox(step.annotated_url!)}
                className="mx-3 mb-1 self-start rounded-md border border-border/60 bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground transition-colors hover:text-foreground"
                title="The annotated set-of-marks image the agent actually saw"
              >
                ◉ Agent view
              </button>
            )}
            {step.payload.next_goal && (
              <p className="px-3 py-2.5 text-sm leading-relaxed text-foreground/90">
                {step.payload.next_goal}
              </p>
            )}
            {action === "ai_look" && step.payload.memory && (
              <p className="px-3 pb-2.5 text-xs text-muted-foreground">{step.payload.memory}</p>
            )}
          </div>
        )
      })}
      {steps.length === 0 && (
        <p className="col-span-full py-10 text-center text-sm text-muted-foreground">
          No steps recorded.
        </p>
      )}
    </div>
  )

  return (
    <div className="space-y-6">
      <div className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <button
              onClick={() => navigate(-1)}
              className="text-muted-foreground transition-colors hover:text-foreground"
            >
              <ArrowLeft className="size-4" />
            </button>
            <h1 className="text-xl font-semibold tracking-tight">Run</h1>
            <span className="font-mono text-xs text-muted-foreground">{runId.slice(0, 8)}</span>
            <OutcomeBadge outcome={run.outcome} lifecycle={run.lifecycle} disputed={run.disputed} />
            {!isLive && <ModeBadge run={run} />}
            {isLive && (
              <span className="flex items-center gap-1.5 text-xs font-medium text-sky-600 dark:text-sky-400">
                <Radio className="size-3 pulse-live" /> live
                <span className="tabular-nums font-mono text-muted-foreground" title="elapsed">
                  {elapsedLabel}
                </span>
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="outline" size="sm" className="gap-1.5" asChild>
              <Link to={`/tests/${run.test_id}`}>
                <FlaskConical className="size-3.5" /> Test
              </Link>
            </Button>
            {!isLive && (
              <Button
                size="sm"
                className="gap-1.5"
                disabled={rerun.isPending}
                onClick={() => rerun.mutate(run.test_id)}
              >
                <RotateCw className="size-3.5" /> Rerun
              </Button>
            )}
          </div>
        </div>

        {/* while live the hero isn't shown yet, so surface progress here */}
        {isLive && run.status_message && (
          <p className="text-sm text-foreground/80">{run.status_message}</p>
        )}

        {/* subtle timing strip next to the title */}
        {run.usage?.timings ? (
          <div className="flex items-center gap-1 text-xs text-muted-foreground" title="execute · verify">
            <Timer className="size-3" />
            {run.usage.timings.execute_s ?? "?"}s exec · {run.usage.timings.verify_s ?? "?"}s verify
          </div>
        ) : (
          run.usage?.duration_s != null && (
            <div className="flex items-center gap-1 text-xs text-muted-foreground">
              <Timer className="size-3" /> {run.usage.duration_s}s
            </div>
          )
        )}
      </div>

      {isLive ? (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
          {/* live view — the hero while running */}
          <Card className="flex flex-col overflow-hidden lg:col-span-3">
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center gap-2 text-sm">
                <MonitorPlay className="size-4 text-sky-500" /> Live view
                <span className="ml-auto flex items-center gap-1.5 font-mono text-[10px] text-muted-foreground">
                  <Radio className="size-3 pulse-live text-sky-500" /> streaming
                </span>
              </CardTitle>
            </CardHeader>
            <CardContent className="flex min-h-[400px] flex-1 items-center justify-center rounded-b bg-black/90">
              {frame ? (
                <img
                  src={frame}
                  alt="live view"
                  className="max-h-[calc(100vh-16rem)] max-w-full rounded"
                />
              ) : (
                <div className="text-sm text-muted-foreground">Connecting live view…</div>
              )}
            </CardContent>
          </Card>

          {/* steps — scroll independently, matched height */}
          <Card className="flex flex-col lg:col-span-2">
            {stepStreamHeader}
            <CardContent className="min-h-0 flex-1">
              <ScrollArea className="h-[calc(100vh-16rem)] min-h-[400px] pr-3">
                {stepList}
              </ScrollArea>
            </CardContent>
          </Card>
        </div>
      ) : (
        <div className="space-y-6">
          {/* OUTCOME — the hero: what happened, and why */}
          <OutcomeHero run={run} />

          {/* HOW — fast replay vs AI, and how each part of the test was verified */}
          <FastReplayCard run={run} />
          {run.clauses && run.clauses.length > 0 && <ChecklistCard clauses={run.clauses} />}

          {/* CONTEXT — compact meta + objective, one scannable strip */}
          <Card>
            <CardContent className="space-y-4 p-4">
              <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                <MetaItem label="Test">
                  <Link to={`/tests/${run.test_id}`} className="text-primary hover:underline">
                    {test?.name ?? "View test"}
                  </Link>
                </MetaItem>
                <MetaItem label="Device">
                  {test ? `${test.device || "iPhone 15"} · ${test.ios_target ?? "simulator"}` : "—"}
                </MetaItem>
                <MetaItem label="Duration">
                  {run.usage?.duration_s != null ? `${run.usage.duration_s}s` : "—"}
                </MetaItem>
                <MetaItem label="Tokens">
                  {run.usage?.total_tokens != null
                    ? `${(run.usage.total_tokens / 1000).toFixed(1)}k${
                        run.usage.judge_tokens != null
                          ? ` · judge ${(run.usage.judge_tokens / 1000).toFixed(1)}k`
                          : ""
                      }`
                    : "—"}
                </MetaItem>
              </div>
              {test?.instructions && (
                <div className="border-t pt-3">
                  <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                    Objective
                  </p>
                  <p className="mt-1 whitespace-pre-wrap text-[13px] leading-relaxed text-foreground/70">
                    {test.instructions}
                  </p>
                </div>
              )}
            </CardContent>
          </Card>

          {/* JOURNEY — the star: filmstrip of every step, full width, one scroll */}
          <section className="space-y-3">
            <div className="flex items-center gap-2">
              <Route className="size-4 text-primary" />
              <h2 className="text-sm font-semibold">Journey</h2>
              {steps.length > 0 && (
                <span className="text-xs text-muted-foreground">{steps.length} steps</span>
              )}
            </div>
            {stepGrid}
          </section>

          {/* RECORDING */}
          {run.video_url && (
            <section className="space-y-3">
              <div className="flex items-center gap-2">
                <Film className="size-4 text-primary" />
                <h2 className="text-sm font-semibold">Recording</h2>
              </div>
              <Card className="overflow-hidden">
                <CardContent className="flex justify-center bg-muted/40 p-4">
                  <RecordingPlayer url={run.video_url} />
                </CardContent>
              </Card>
            </section>
          )}

          {/* AGENT SELF-REPORT — a footnote now that the hero owns the verdict */}
          {run.self_verdict && (
            <VerdictCard
              title="Agent self-report"
              icon={<Bot className="size-4 text-muted-foreground" />}
              verdict={run.self_verdict}
              emptyHint=""
              className={cn(run.disputed && "border-violet-500/30")}
            />
          )}
        </div>
      )}

      {lightbox && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/80 p-6"
          onClick={() => setLightbox(null)}
        >
          <img
            src={lightbox}
            alt="screenshot"
            className="max-h-full max-w-full rounded shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          />
        </div>
      )}
    </div>
  )
}

function MetaItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="mt-0.5 truncate text-sm font-medium">{children}</p>
    </div>
  )
}

const OUTCOME = {
  passed: { chip: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400", border: "border-l-emerald-500", label: "Passed", Icon: Check },
  failed: { chip: "bg-rose-500/10 text-rose-600 dark:text-rose-400", border: "border-l-rose-500", label: "Failed", Icon: X },
  error: { chip: "bg-amber-500/10 text-amber-600 dark:text-amber-400", border: "border-l-amber-500", label: "Error", Icon: TriangleAlert },
} as const

/** Who decided the verdict the deterministic checks left open. Older runs have no
 * verdict_source and were always judged. */
function verdictSource(run: TestRun): "agent" | "judge" {
  return run.spec_snapshot?.verdict_source ?? "judge"
}

/** Who decided the verdict, in one line — true for fast replays, failures and errors too. */
function decidedBy(run: TestRun, source: "agent" | "judge"): string {
  const fast = run.graduation?.mode === "fast"
  if (run.outcome === "error" || !run.outcome) return "No verdict could be reached"
  if (source === "judge") return "Decided by the independent judge"
  if (run.outcome === "failed" && run.self_verdict?.passed)
    return `Decided by a deterministic check, which overrides the ${fast ? "replay's" : "agent's"} pass`
  if (fast)
    return run.outcome === "passed"
      ? "Decided by fast replay: the recorded steps were replayed and every check held on the replayed screen"
      : "Decided by fast replay: a locked check failed on the replayed screen"
  if (run.outcome === "passed")
    return "Decided by the agent, after every checklist clause was backed by on-screen evidence and all checks passed"
  return "Decided by the agent from what it observed — see the checklist"
}

function OutcomeHero({ run }: { run: TestRun }) {
  const o = OUTCOME[(run.outcome ?? "error") as keyof typeof OUTCOME] ?? OUTCOME.error
  const source = verdictSource(run)
  const v = source === "judge" ? run.judge_verdict : run.self_verdict
  const reason = run.status_message || v?.reason
  const repro = v?.repro_steps ?? []
  return (
    <Card className={cn("border-l-4", o.border)}>
      <CardContent className="space-y-4 p-5">
        <div className="flex items-start gap-3">
          <span className={cn("flex size-11 shrink-0 items-center justify-center rounded-xl", o.chip)}>
            <o.Icon className="size-5" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="text-lg font-semibold tracking-tight">{o.label}</h2>
              {run.disputed && (
                <span className="rounded bg-violet-500/10 px-1.5 py-0.5 text-[11px] font-medium text-violet-600 dark:text-violet-400">
                  disputed
                </span>
              )}
            </div>
            {reason && <p className="mt-0.5 text-sm text-foreground/80">{reason}</p>}
            <p className="mt-1 text-[11px] text-muted-foreground">{decidedBy(run, source)}</p>
          </div>
        </div>
        {v?.reasoning && (
          <div className="rounded-lg bg-muted/40 p-3">
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Why</p>
            <p className="mt-1 text-sm leading-relaxed text-foreground/80">{v.reasoning}</p>
          </div>
        )}
        {repro.length > 0 && (
          <div>
            <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">Repro steps</p>
            <ol className="mt-1.5 list-inside list-decimal space-y-1 text-sm text-foreground/80">
              {repro.map((s, i) => (
                <li key={i}>{s}</li>
              ))}
            </ol>
          </div>
        )}
        {run.disputed && (
          <p className="flex items-center gap-1.5 border-t pt-3 text-xs text-violet-600 dark:text-violet-400">
            <ShieldCheck className="size-3.5" /> The agent self-reported differently — the judge's verdict is authoritative.
          </p>
        )}
      </CardContent>
    </Card>
  )
}

const ACTION_STYLES: Record<string, string> = {
  tap: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  click: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  type: "bg-violet-500/10 text-violet-600 dark:text-violet-400",
  swipe: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  scroll: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
  navigate: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  done: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  ai_look: "bg-fuchsia-500/10 text-fuchsia-600 dark:text-fuchsia-400",
}

function ActionBadge({ action }: { action: string }) {
  return (
    <span
      className={cn(
        "rounded px-1.5 py-0.5 font-mono text-[10px] font-medium",
        ACTION_STYLES[action] ?? "bg-muted text-muted-foreground",
      )}
    >
      {action}
    </span>
  )
}

function VerdictCard({
  title,
  icon,
  verdict,
  emptyHint,
  authoritative,
  className,
}: {
  title: string
  icon: React.ReactNode
  verdict: Verdict | null
  emptyHint: string
  authoritative?: boolean
  className?: string
}) {
  return (
    <Card className={cn(className)}>
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center justify-between text-sm">
          <span className="flex items-center gap-2">
            {icon} {title}
          </span>
          {authoritative && verdict && (
            <span className="font-mono text-[10px] text-primary">authoritative</span>
          )}
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {verdict ? (
          <>
            <p
              className={cn(
                "font-medium",
                verdict.passed ? "text-emerald-600 dark:text-emerald-400" : "text-red-600 dark:text-red-400",
              )}
            >
              {verdict.passed ? "PASS" : "FAIL"} — {verdict.reason}
            </p>
            {verdict.reasoning && (
              <p className="text-xs text-muted-foreground">{verdict.reasoning}</p>
            )}
            {verdict.confidence != null && (
              <p className="font-mono text-[10px] text-muted-foreground">
                confidence {(verdict.confidence * 100).toFixed(0)}%
              </p>
            )}
            {verdict.repro_steps && verdict.repro_steps.length > 0 && (
              <>
                <Separator />
                <p className="text-xs font-medium">Repro steps</p>
                <ol className="list-inside list-decimal space-y-0.5 text-xs text-muted-foreground">
                  {verdict.repro_steps.map((step, i) => (
                    <li key={i}>{step}</li>
                  ))}
                </ol>
              </>
            )}
          </>
        ) : (
          <p className="text-xs text-muted-foreground">{emptyHint}</p>
        )}
      </CardContent>
    </Card>
  )
}

/** Cloud recordings can take a few seconds to finalize, so the first loads may
 * fail. Show a processing state and retry a few times by remounting the <video>.
 * NOTE: the src is a SIGNED url (presigned S3 or our HMAC token) — never append
 * query params to it, that invalidates the signature. Bump `key` to re-request
 * the same url instead. */
function RecordingPlayer({ url }: { url: string }) {
  const [ready, setReady] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const maxAttempts = 6

  return (
    <div className="relative mx-auto min-h-[160px]">
      {!ready && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2 rounded bg-muted/30 text-xs text-muted-foreground">
          <Radio className="size-4 animate-pulse" />
          {attempt < maxAttempts ? "Preparing recording…" : "Recording unavailable"}
        </div>
      )}
      <video
        key={attempt}
        src={url}
        controls
        onLoadedData={() => setReady(true)}
        onError={() => {
          if (attempt < maxAttempts) setTimeout(() => setAttempt((a) => a + 1), 3000)
        }}
        className={cn(
          "mx-auto max-h-[70vh] w-auto rounded bg-black",
          !ready && "invisible",
        )}
      />
    </div>
  )
}
