import { useState } from "react"
import { Link, useNavigate, useParams } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api, type Test } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Card } from "@/components/ui/card"
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { RunRow, StatCard, runMetrics } from "@/components/stats"
import { TestFormDialog } from "@/components/test-form-dialog"
import {
  Check as CheckIcon,
  FlaskConical,
  Gauge,
  Package,
  Pencil,
  Play,
  Plus,
  Smartphone,
  Trash2,
  X,
  Zap,
} from "lucide-react"

export function ProjectDetailPage() {
  const { projectId = "" } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()

  const { data: project } = useQuery({ queryKey: ["project", projectId], queryFn: () => api.projects.get(projectId) })
  const { data: tests } = useQuery({ queryKey: ["tests", projectId], queryFn: () => api.projects.tests(projectId) })
  const { data: runs } = useQuery({
    queryKey: ["runs", projectId],
    queryFn: () => api.projects.runs(projectId),
    refetchInterval: (q) => (q.state.data?.some((r) => r.lifecycle !== "finished") ? 4000 : false),
  })
  const { data: builds } = useQuery({ queryKey: ["builds", projectId], queryFn: () => api.projects.builds(projectId) })

  const [open, setOpen] = useState(false)
  const [editing, setEditing] = useState<Test | null>(null)

  const invalidateTests = () => queryClient.invalidateQueries({ queryKey: ["tests", projectId] })
  const deleteTest = useMutation({ mutationFn: (id: string) => api.tests.remove(id), onSuccess: invalidateTests })
  const runTest = useMutation({
    mutationFn: (testId: string) => api.tests.run(testId),
    onSuccess: (run) => navigate(`/runs/${run.id}`),
  })

  const invalidateBuilds = () => queryClient.invalidateQueries({ queryKey: ["builds", projectId] })
  const [buildForm, setBuildForm] = useState({ name: "", app_path: "", bundle_id: "" })
  const [buildFile, setBuildFile] = useState<File | null>(null)
  const [buildOpen, setBuildOpen] = useState(false)
  const addBuild = useMutation({
    mutationFn: () =>
      buildFile
        ? api.builds.upload(projectId, { file: buildFile, name: buildForm.name, bundle_id: buildForm.bundle_id })
        : api.builds.create(projectId, buildForm),
    onSuccess: () => {
      invalidateBuilds()
      setBuildOpen(false)
      setBuildForm({ name: "", app_path: "", bundle_id: "" })
      setBuildFile(null)
    },
  })
  const deleteBuild = useMutation({ mutationFn: (id: string) => api.builds.remove(id), onSuccess: invalidateBuilds })

  const openCreate = () => {
    setEditing(null)
    setOpen(true)
  }
  const openEdit = (t: Test) => {
    setEditing(t)
    setOpen(true)
  }

  const testName = (id: string) => tests?.find((t) => t.id === id)?.name ?? id.slice(0, 8)
  const m = runMetrics(runs ?? [])

  return (
    <div className="mx-auto max-w-5xl space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">{project?.name}</h1>
          <p className="mt-0.5 flex items-center gap-1.5 font-mono text-xs text-muted-foreground">
            <Smartphone className="size-3" /> iOS
          </p>
        </div>
        <Button className="gap-2" onClick={openCreate}>
          <Plus className="size-4" /> New test
        </Button>
        <TestFormDialog
          projectId={projectId}
          open={open}
          onOpenChange={setOpen}
          editing={editing}
        />
      </div>

      {/* metrics */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatCard label="Total tests" value={tests?.length ?? 0} icon={<FlaskConical className="size-4" />} tone="primary" />
        <StatCard
          label="Pass rate"
          value={m.passRate === null ? "—" : `${m.passRate}%`}
          sub={m.decided ? `${m.passed}/${m.decided} runs` : "no runs yet"}
          icon={<Gauge className="size-4" />}
          tone="sky"
          progress={m.passRate}
        />
        <StatCard label="Passed" value={m.passed} icon={<CheckIcon className="size-4" />} tone="emerald" />
        <StatCard label="Failed" value={m.failed} icon={<X className="size-4" />} tone="rose" />
      </div>

      <Tabs defaultValue="tests">
        <TabsList>
          <TabsTrigger value="tests">Tests {tests?.length ? `(${tests.length})` : ""}</TabsTrigger>
          <TabsTrigger value="runs">Runs {runs?.length ? `(${runs.length})` : ""}</TabsTrigger>
          <TabsTrigger value="builds">Builds {builds?.length ? `(${builds.length})` : ""}</TabsTrigger>
        </TabsList>

        {/* TESTS — a clean list; row → test detail */}
        <TabsContent value="tests" className="mt-4">
          <Card className="divide-y divide-border/60 py-0">
            {(tests ?? []).map((test) => (
              <div key={test.id} className="flex items-center gap-3 px-4 py-3 hover:bg-muted/40">
                <Link to={`/tests/${test.id}`} className="flex min-w-0 flex-1 items-center gap-3">
                  <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <FlaskConical className="size-4" />
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{test.name}</p>
                    <p className="truncate text-sm text-muted-foreground">{test.instructions}</p>
                  </div>
                </Link>
                <span className="hidden shrink-0 font-mono text-[11px] text-muted-foreground sm:block">
                  {test.device || "iPhone 15"}{test.ios_platform_version ? ` · iOS ${test.ios_platform_version}` : ""} · {test.ios_target ?? "simulator"}
                </span>
                <Button size="icon" variant="ghost" className="size-7 text-muted-foreground" title="Edit" onClick={() => openEdit(test)}>
                  <Pencil className="size-3.5" />
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  className="size-7 text-muted-foreground hover:text-destructive"
                  title="Delete"
                  disabled={deleteTest.isPending}
                  onClick={() => confirm(`Delete "${test.name}" and its runs?`) && deleteTest.mutate(test.id)}
                >
                  <Trash2 className="size-3.5" />
                </Button>
                <Button size="sm" variant="outline" className="gap-1.5" disabled={runTest.isPending} onClick={() => runTest.mutate(test.id)}>
                  <Play className="size-3" /> Run
                </Button>
              </div>
            ))}
            {tests?.length === 0 && (
              <p className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
                <FlaskConical className="size-5 opacity-40" /> No tests yet — create one to get started.
              </p>
            )}
          </Card>
        </TabsContent>

        {/* RUNS — activity across the project */}
        <TabsContent value="runs" className="mt-4">
          <Card className="divide-y divide-border/60 py-0">
            {(runs ?? []).slice(0, 30).map((run) => (
              <RunRow key={run.id} run={run} title={testName(run.test_id)} />
            ))}
            {runs?.length === 0 && (
              <p className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
                <Zap className="size-5 opacity-40" /> No runs yet.
              </p>
            )}
          </Card>
        </TabsContent>

        {/* BUILDS */}
        <TabsContent value="builds" className="mt-4 space-y-3">
          <div className="flex justify-end">
            <Dialog open={buildOpen} onOpenChange={setBuildOpen}>
              <DialogTrigger asChild>
                <Button size="sm" variant="outline" className="gap-1.5">
                  <Plus className="size-3" /> Add build
                </Button>
              </DialogTrigger>
              <DialogContent className="max-w-md">
                <DialogHeader>
                  <DialogTitle>Add build</DialogTitle>
                </DialogHeader>
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label>App file (.ipa / .app.zip)</Label>
                    <Input
                      type="file"
                      accept=".ipa,.zip,.app"
                      className="text-xs file:mr-3 file:rounded file:border-0 file:bg-primary/10 file:px-2 file:py-1 file:text-primary"
                      onChange={(e) => {
                        const f = e.target.files?.[0] ?? null
                        setBuildFile(f)
                        if (f && !buildForm.name) setBuildForm({ ...buildForm, name: f.name.replace(/\.(ipa|zip|app)$/i, "") })
                      }}
                    />
                    {buildFile && (
                      <p className="text-[11px] text-muted-foreground">
                        {buildFile.name} · {(buildFile.size / 1048576).toFixed(1)} MB
                      </p>
                    )}
                  </div>
                  {!buildFile && (
                    <div className="space-y-2">
                      <Label className="text-muted-foreground">…or a local path / vendor ref</Label>
                      <Input
                        className="font-mono text-xs"
                        value={buildForm.app_path}
                        onChange={(e) => setBuildForm({ ...buildForm, app_path: e.target.value })}
                        placeholder="/path/to/App.ipa  or  bs://…"
                      />
                    </div>
                  )}
                  <div className="space-y-2">
                    <Label>Name</Label>
                    <Input value={buildForm.name} onChange={(e) => setBuildForm({ ...buildForm, name: e.target.value })} placeholder="MyApp 1.0" />
                  </div>
                  <div className="space-y-2">
                    <Label>Bundle id (optional)</Label>
                    <Input
                      className="font-mono text-xs"
                      value={buildForm.bundle_id}
                      onChange={(e) => setBuildForm({ ...buildForm, bundle_id: e.target.value })}
                      placeholder="com.example.myapp"
                    />
                  </div>
                  {addBuild.isError && (
                    <p className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">{(addBuild.error as Error).message}</p>
                  )}
                  <Button
                    className="w-full"
                    disabled={!buildForm.name || (!buildFile && !buildForm.app_path) || addBuild.isPending}
                    onClick={() => addBuild.mutate()}
                  >
                    {addBuild.isPending ? "Uploading…" : "Add build"}
                  </Button>
                </div>
              </DialogContent>
            </Dialog>
          </div>
          <Card className="divide-y divide-border/60 py-0">
            {(builds ?? []).map((b) => (
              <div key={b.id} className="flex items-center gap-3 px-4 py-3">
                <span className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                  <Package className="size-4" />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium">{b.name}</p>
                  <p className="truncate font-mono text-[11px] text-muted-foreground">{b.bundle_id ?? b.app_path.split("/").pop()}</p>
                </div>
                <Button
                  size="icon"
                  variant="ghost"
                  className="size-7 shrink-0 text-muted-foreground hover:text-destructive"
                  title="Delete build"
                  disabled={deleteBuild.isPending}
                  onClick={() => confirm(`Delete build "${b.name}"?`) && deleteBuild.mutate(b.id)}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </div>
            ))}
            {builds?.length === 0 && (
              <p className="flex flex-col items-center gap-2 py-10 text-center text-sm text-muted-foreground">
                <Package className="size-5 opacity-40" /> No builds yet — add one to reuse across tests.
              </p>
            )}
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  )
}
