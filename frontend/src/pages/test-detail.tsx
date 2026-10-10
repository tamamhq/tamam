import { useState } from "react"
import { useNavigate, useParams } from "react-router-dom"
import { useMutation, useQuery } from "@tanstack/react-query"
import { api } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card, CardContent } from "@/components/ui/card"
import { RunRow, StatCard, runMetrics } from "@/components/stats"
import { TestFormDialog } from "@/components/test-form-dialog"
import { parseServerTime } from "@/lib/utils"
import { ArrowLeft, Check, Clock, Gauge, Pencil, Play, Smartphone, X, Zap } from "lucide-react"

export function TestDetailPage() {
  const { testId = "" } = useParams()
  const navigate = useNavigate()
  const [editOpen, setEditOpen] = useState(false)

  const { data: test } = useQuery({ queryKey: ["test", testId], queryFn: () => api.tests.get(testId) })
  const { data: runs } = useQuery({
    queryKey: ["test-runs", testId],
    queryFn: () => api.tests.runs(testId),
    refetchInterval: (q) => (q.state.data?.some((r) => r.lifecycle !== "finished") ? 4000 : false),
  })

  const runTest = useMutation({
    mutationFn: () => api.tests.run(testId),
    onSuccess: (run) => navigate(`/runs/${run.id}`),
  })

  if (!test) return null
  const m = runMetrics(runs ?? [])

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <button
            onClick={() => navigate(`/projects/${test.project_id}`)}
            className="flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            <ArrowLeft className="size-3" /> Back to project
          </button>
          <h1 className="mt-1 text-2xl font-semibold tracking-tight">{test.name}</h1>
          <p className="mt-1 flex flex-wrap items-center gap-2 font-mono text-xs text-muted-foreground">
            <span className="flex items-center gap-1">
              <Smartphone className="size-3" /> {test.device || "iPhone 15"}{test.ios_platform_version ? ` · iOS ${test.ios_platform_version}` : ""}
            </span>
            <span>· {test.ios_target ?? "simulator"}</span>
            {test.app_bundle_id && <span>· {test.app_bundle_id}</span>}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" className="gap-2" onClick={() => setEditOpen(true)}>
            <Pencil className="size-3.5" /> Edit
          </Button>
          <Button className="gap-2" disabled={runTest.isPending} onClick={() => runTest.mutate()}>
            <Play className="size-4" /> Run test
          </Button>
        </div>
      </div>

      <TestFormDialog
        projectId={test.project_id}
        open={editOpen}
        onOpenChange={setEditOpen}
        editing={test}
      />

      {/* objective */}
      <Card>
        <CardContent className="p-4">
          <p className="text-xs font-medium text-muted-foreground">Objective</p>
          <p className="mt-1 whitespace-pre-wrap text-sm">{test.instructions}</p>
        </CardContent>
      </Card>

      {/* per-test metrics */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="Runs" value={m.total} icon={<Zap className="size-4" />} tone="primary" />
        <StatCard
          label="Pass rate"
          value={m.passRate === null ? "—" : `${m.passRate}%`}
          sub={m.decided ? `${m.passed}/${m.decided}` : "no runs"}
          icon={<Gauge className="size-4" />}
          tone="sky"
          progress={m.passRate}
        />
        <StatCard label="Passed" value={m.passed} icon={<Check className="size-4" />} tone="emerald" />
        <StatCard
          label="Avg duration"
          value={m.avgDuration === null ? "—" : `${m.avgDuration}s`}
          icon={<Clock className="size-4" />}
          tone="amber"
        />
      </div>

      {/* run history */}
      <div className="space-y-3">
        <h2 className="text-sm font-medium text-muted-foreground">Run history</h2>
        <Card className="divide-y divide-border/60 py-0">
          {(runs ?? []).map((run) => (
            <RunRow key={run.id} run={run} title={new Date(parseServerTime(run.created_at)).toLocaleString()} />
          ))}
          {runs?.length === 0 && (
            <p className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
              <X className="size-5 opacity-40" />
              No runs yet — hit “Run test” to start.
            </p>
          )}
        </Card>
      </div>
    </div>
  )
}
