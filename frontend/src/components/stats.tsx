import type { ReactNode } from "react"
import { useNavigate } from "react-router-dom"
import { Card, CardContent } from "@/components/ui/card"
import type { TestRun } from "@/lib/api"
import { cn, parseServerTime } from "@/lib/utils"
import { Check, ChevronRight, Loader2, TriangleAlert, X } from "lucide-react"

const TONES = {
  primary: "bg-primary/10 text-primary",
  sky: "bg-sky-500/10 text-sky-600 dark:text-sky-400",
  emerald: "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  rose: "bg-rose-500/10 text-rose-600 dark:text-rose-400",
  amber: "bg-amber-500/10 text-amber-600 dark:text-amber-400",
} as const

/** Small progress ring (0–100), colored by health. */
function Donut({ value, size = 24 }: { value: number; size?: number }) {
  const r = (size - 3) / 2
  const c = 2 * Math.PI * r
  const dash = Math.max(0, Math.min(100, value)) / 100
  const tone = value >= 80 ? "text-emerald-500" : value >= 50 ? "text-amber-500" : "text-rose-500"
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} className="shrink-0 -rotate-90">
      <circle cx={size / 2} cy={size / 2} r={r} fill="none" strokeWidth="3" className="stroke-muted" />
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        strokeWidth="3"
        strokeLinecap="round"
        stroke="currentColor"
        strokeDasharray={`${dash * c} ${c}`}
        className={tone}
      />
    </svg>
  )
}

export function StatCard({
  label,
  value,
  sub,
  icon,
  tone = "primary",
  progress,
}: {
  label: string
  value: string | number
  sub?: string
  icon: ReactNode
  tone?: keyof typeof TONES
  /** When set, render a health donut (0–100) in place of the icon chip. */
  progress?: number | null
}) {
  return (
    <Card className="gap-0 py-0">
      <CardContent className="p-4">
        <div className="flex items-center gap-2">
          {progress != null ? (
            <Donut value={progress} />
          ) : (
            <span className={cn("flex size-6 shrink-0 items-center justify-center rounded-md", TONES[tone])}>
              {icon}
            </span>
          )}
          <p className="text-sm font-medium text-muted-foreground">{label}</p>
        </div>
        <p className="mt-2.5 text-3xl font-semibold leading-none tracking-tight tabular-nums">{value}</p>
        {sub && <p className="mt-1.5 text-xs text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  )
}

/** Tinted status chip for a run — same visual language as the metric icon chips. */
export function RunStatusIcon({ run, size = "md" }: { run: TestRun; size?: "sm" | "md" }) {
  const cls = cn(
    "flex shrink-0 items-center justify-center rounded-lg",
    size === "sm" ? "size-7" : "size-9",
  )
  const icon = size === "sm" ? "size-3.5" : "size-4"
  if (run.lifecycle !== "finished")
    return (
      <span className={cn(cls, "bg-sky-500/10 text-sky-600 dark:text-sky-400")}>
        <Loader2 className={cn(icon, "animate-spin")} />
      </span>
    )
  if (run.outcome === "passed")
    return (
      <span className={cn(cls, "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400")}>
        <Check className={icon} />
      </span>
    )
  if (run.outcome === "failed")
    return (
      <span className={cn(cls, "bg-rose-500/10 text-rose-600 dark:text-rose-400")}>
        <X className={icon} />
      </span>
    )
  return (
    <span className={cn(cls, "bg-amber-500/10 text-amber-600 dark:text-amber-400")}>
      <TriangleAlert className={icon} />
    </span>
  )
}

/** A clickable run row for activity lists. */
export function RunRow({ run, title }: { run: TestRun; title: string }) {
  const navigate = useNavigate()
  return (
    <button
      onClick={() => navigate(`/runs/${run.id}`)}
      className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/40"
    >
      <RunStatusIcon run={run} />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{title}</p>
        <p className="truncate text-sm text-muted-foreground">{run.status_message || "—"}</p>
      </div>
      <div className="hidden shrink-0 text-right sm:block">
        <p className="text-xs text-muted-foreground">
          {new Date(parseServerTime(run.created_at)).toLocaleString([], {
            month: "short",
            day: "numeric",
            hour: "2-digit",
            minute: "2-digit",
          })}
        </p>
        {run.usage?.duration_s != null && (
          <p className="font-mono text-[11px] text-muted-foreground">{run.usage.duration_s}s</p>
        )}
      </div>
      <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
    </button>
  )
}

/** Compute pass-rate metrics from a list of runs. */
export function runMetrics(runs: TestRun[]) {
  const finished = runs.filter((r) => r.lifecycle === "finished")
  const passed = finished.filter((r) => r.outcome === "passed").length
  const failed = finished.filter((r) => r.outcome === "failed").length
  const decided = passed + failed
  const durations = finished.map((r) => r.usage?.duration_s).filter((d): d is number => d != null)
  const avgDuration = durations.length
    ? Math.round(durations.reduce((a, b) => a + b, 0) / durations.length)
    : null
  return {
    total: runs.length,
    passed,
    failed,
    decided,
    passRate: decided ? Math.round((passed / decided) * 100) : null,
    avgDuration,
  }
}
