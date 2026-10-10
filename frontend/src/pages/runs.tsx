import { useQuery } from "@tanstack/react-query"
import { api, type TestRun } from "@/lib/api"
import { Card } from "@/components/ui/card"
import { RunRow, StatCard, runMetrics } from "@/components/stats"
import { Check, Gauge, X, Zap } from "lucide-react"

export function RunsPage() {
  // Fan out across the org's projects → their runs (there's no global runs endpoint
  // yet; this aggregates client-side, newest first).
  const { data: projects } = useQuery({ queryKey: ["projects"], queryFn: api.projects.list })
  const results = useQuery({
    queryKey: ["all-runs", projects?.map((p) => p.id)],
    enabled: !!projects,
    queryFn: async () => {
      const [runLists, testLists] = await Promise.all([
        Promise.all((projects ?? []).map((p) => api.projects.runs(p.id))),
        Promise.all((projects ?? []).map((p) => api.projects.tests(p.id))),
      ])
      const testName = new Map(testLists.flat().map((t) => [t.id, t.name]))
      const flat = runLists.flat().sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
      return { flat, testName }
    },
    refetchInterval: (q) => (q.state.data?.flat.some((r) => r.lifecycle !== "finished") ? 4000 : false),
  })

  const runs: TestRun[] = results.data?.flat ?? []
  const testName = results.data?.testName ?? new Map<string, string>()
  const m = runMetrics(runs)

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Runs</h1>
        <p className="mt-0.5 text-sm text-muted-foreground">Recent test executions across your projects</p>
      </div>

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="Total runs" value={runs.length} icon={<Zap className="size-4" />} tone="primary" />
        <StatCard
          label="Pass rate"
          value={m.passRate === null ? "—" : `${m.passRate}%`}
          sub={m.decided ? `${m.passed}/${m.decided}` : "no runs"}
          icon={<Gauge className="size-4" />}
          tone="sky"
          progress={m.passRate}
        />
        <StatCard label="Passed" value={m.passed} icon={<Check className="size-4" />} tone="emerald" />
        <StatCard label="Failed" value={m.failed} icon={<X className="size-4" />} tone="rose" />
      </div>

      <Card className="divide-y divide-border/60 py-0">
        {runs.slice(0, 50).map((run) => (
          <RunRow key={run.id} run={run} title={testName.get(run.test_id) ?? "Test"} />
        ))}
        {runs.length === 0 && (
          <p className="flex flex-col items-center gap-2 py-12 text-center text-sm text-muted-foreground">
            <Zap className="size-5 opacity-40" /> No runs yet.
          </p>
        )}
      </Card>
    </div>
  )
}
