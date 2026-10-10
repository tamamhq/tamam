import { Badge } from "@/components/ui/badge"
import { cn } from "@/lib/utils"
import type { Lifecycle, Outcome } from "@/lib/api"
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Loader2,
  Scale,
  XCircle,
} from "lucide-react"

export function OutcomeBadge({
  outcome,
  lifecycle,
  disputed,
  className,
}: {
  outcome: Outcome
  lifecycle: Lifecycle
  disputed?: boolean
  className?: string
}) {
  if (lifecycle !== "finished") {
    return (
      <Badge
        variant="outline"
        className={cn(
          "gap-1.5 border-sky-500/30 bg-sky-500/10 text-sky-600 dark:text-sky-400",
          className,
        )}
      >
        {lifecycle === "queued" ? (
          <CircleDashed className="size-3" />
        ) : (
          <Loader2 className="size-3 animate-spin" />
        )}
        {lifecycle}
      </Badge>
    )
  }

  if (disputed)
    return (
      <Badge
        variant="outline"
        className={cn(
          "gap-1.5 border-violet-500/30 bg-violet-500/10 text-violet-600 dark:text-violet-400",
          className,
        )}
      >
        <Scale className="size-3" />
        disputed · {outcome}
      </Badge>
    )

  const map = {
    passed: {
      icon: CheckCircle2,
      cls: "border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
    },
    failed: {
      icon: XCircle,
      cls: "border-red-500/30 bg-red-500/10 text-red-600 dark:text-red-400",
    },
    error: {
      icon: AlertTriangle,
      cls: "border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400",
    },
  } as const

  const entry = outcome ? map[outcome] : null
  if (!entry) return null
  const Icon = entry.icon
  return (
    <Badge variant="outline" className={cn("gap-1.5", entry.cls, className)}>
      <Icon className="size-3" />
      {outcome}
    </Badge>
  )
}
