import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { api, type Check, type Test } from "@/lib/api"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Textarea } from "@/components/ui/textarea"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Smartphone } from "lucide-react"

const emptyForm = {
  name: "",
  instructions: "",
  start_url: "",
  executor_id: "ios_agent",
  build_id: "",
  app_build_ref: "",
  app_bundle_id: "",
  device: "",
  ios_platform_version: "",
  ios_target: "simulator",
  reuse_session: false,
  checks: [] as Check[],
  checks_exhaustive: false,
}

function formFrom(test: Test | null | undefined) {
  if (!test) return { ...emptyForm }
  return {
    ...emptyForm,
    name: test.name,
    instructions: test.instructions,
    executor_id: "ios_agent",
    build_id: test.build_id ?? "",
    app_bundle_id: test.app_bundle_id ?? "",
    device: test.device ?? "",
    ios_platform_version: test.ios_platform_version ?? "",
    ios_target: test.ios_target ?? "simulator",
    reuse_session: test.reuse_session ?? false,
  }
}

/** Create/edit test dialog — the single source of truth for the test form, shared
 * by the project (list) page and the test-detail page. Pass `editing` to edit. */
export function TestFormDialog({
  projectId,
  open,
  onOpenChange,
  editing,
  onSaved,
}: {
  projectId: string
  open: boolean
  onOpenChange: (open: boolean) => void
  editing?: Test | null
  onSaved?: () => void
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState(emptyForm)

  // Seed the form each time the dialog opens (create → empty, edit → from test).
  useEffect(() => {
    if (open) setForm(formFrom(editing))
  }, [open, editing])

  const { data: builds } = useQuery({
    queryKey: ["builds", projectId],
    queryFn: () => api.projects.builds(projectId),
    enabled: open,
  })
  const { data: iosDevices } = useQuery({
    queryKey: ["ios-devices"],
    queryFn: () => api.ios.devices(),
    enabled: open,
    staleTime: 6 * 3600 * 1000,
  })
  const versionsFor = (device: string) =>
    iosDevices?.find((d) => d.device === device)?.versions ?? []

  // On cloud, default device + OS to the newest the farm offers.
  useEffect(() => {
    if (form.ios_target !== "cloud" || !iosDevices?.length) return
    const known = iosDevices.some((d) => d.device === form.device)
    if (!form.device || !known) {
      const first = iosDevices[0]
      setForm((f) => ({ ...f, device: first.device, ios_platform_version: first.versions[0] ?? "" }))
    } else if (!versionsFor(form.device).includes(form.ios_platform_version)) {
      setForm((f) => ({ ...f, ios_platform_version: versionsFor(form.device)[0] ?? "" }))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [form.ios_target, form.device, iosDevices])

  const navigate = useNavigate()

  const buildPayload = () => ({
    ...form,
    build_id: form.build_id || null,
    ios_platform_version: form.ios_platform_version || null,
  })

  const saveTest = useMutation({
    mutationFn: () =>
      editing ? api.tests.update(editing.id, buildPayload()) : api.tests.create(projectId, buildPayload()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["tests", projectId] })
      if (editing) queryClient.invalidateQueries({ queryKey: ["test", editing.id] })
      onOpenChange(false)
      onSaved?.()
    },
  })

  // Create the test AND immediately run it, then jump to the live run — one click from
  // objective to watching it execute (and it sidesteps hunting for the new test in the list).
  const createAndRun = useMutation({
    mutationFn: async () => {
      const test = await api.tests.create(projectId, buildPayload())
      return api.tests.run(test.id)
    },
    onSuccess: (run) => {
      queryClient.invalidateQueries({ queryKey: ["tests", projectId] })
      onOpenChange(false)
      onSaved?.()
      navigate(`/runs/${run.id}`)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{editing ? "Edit test" : "New test"}</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label>Name</Label>
            <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="checkout completes" />
          </div>
          <div className="space-y-2">
            <Label>Instructions (the objective)</Label>
            <Textarea
              rows={6}
              className="min-h-[150px] md:text-base leading-relaxed"
              value={form.instructions}
              onChange={(e) => setForm({ ...form, instructions: e.target.value })}
              placeholder="Open Settings, tap General, verify the About row appears"
            />
          </div>

          <div className="grid grid-cols-2 gap-4 rounded-md border border-border/60 bg-muted/20 p-3">
            <div className="col-span-2 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Smartphone className="size-3.5 text-primary" /> iOS test — pick the app build and where to run it
            </div>
            <div className="col-span-2 space-y-2">
              <Label>Run on</Label>
              <Select value={form.ios_target} onValueChange={(ios_target) => setForm({ ...form, ios_target })}>
                <SelectTrigger className="text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="simulator" className="text-xs">Simulator — local, free, fast</SelectItem>
                  <SelectItem value="cloud" className="text-xs">Real device — cloud (uses your farm minutes)</SelectItem>
                  <SelectItem value="device" className="text-xs">Real device — attached to this Mac</SelectItem>
                </SelectContent>
              </Select>
            </div>

            {form.ios_target === "cloud" ? (
              <>
                <div className="space-y-2">
                  <Label>Device</Label>
                  <Select
                    value={form.device || undefined}
                    onValueChange={(device) =>
                      setForm({ ...form, device, ios_platform_version: versionsFor(device)[0] ?? "" })
                    }
                  >
                    <SelectTrigger className="text-xs">
                      <SelectValue placeholder="Select a device" />
                    </SelectTrigger>
                    <SelectContent>
                      {(iosDevices ?? []).map((d) => (
                        <SelectItem key={d.device} value={d.device} className="text-xs">{d.device}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label>iOS version</Label>
                  <Select
                    value={form.ios_platform_version || undefined}
                    onValueChange={(v) => setForm({ ...form, ios_platform_version: v })}
                  >
                    <SelectTrigger className="text-xs">
                      <SelectValue placeholder="Newest" />
                    </SelectTrigger>
                    <SelectContent>
                      {versionsFor(form.device).map((v, i) => (
                        <SelectItem key={v} value={v} className="text-xs">
                          iOS {v}{i === 0 ? " (newest)" : ""}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <p className="col-span-2 -mt-1 text-[11px] text-muted-foreground">
                  Only devices &amp; iOS versions your farm actually offers — an app that requires a
                  newer iOS won’t be run on an older one.
                </p>
              </>
            ) : (
              <div className="col-span-2 space-y-2">
                <Label>Device</Label>
                <Input
                  value={form.device}
                  onChange={(e) => setForm({ ...form, device: e.target.value })}
                  placeholder="iPhone 15"
                />
              </div>
            )}
            <div className="col-span-2 space-y-2">
              <Label>App build</Label>
              <Select value={form.build_id || undefined} onValueChange={(v) => setForm({ ...form, build_id: v })}>
                <SelectTrigger className="text-xs">
                  <SelectValue placeholder="Select a build" />
                </SelectTrigger>
                <SelectContent>
                  {(builds ?? []).map((b) => (
                    <SelectItem key={b.id} value={b.id} className="text-xs">{b.name}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>


          {(() => {
            const disabled =
              !form.name ||
              !form.instructions ||
              (!form.build_id && !form.app_bundle_id) ||
              saveTest.isPending ||
              createAndRun.isPending
            if (editing) {
              return (
                <Button className="w-full" disabled={disabled} onClick={() => saveTest.mutate()}>
                  Save changes
                </Button>
              )
            }
            return (
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  className="flex-1"
                  disabled={disabled}
                  onClick={() => saveTest.mutate()}
                >
                  {saveTest.isPending ? "Creating…" : "Create test"}
                </Button>
                <Button
                  className="flex-1"
                  disabled={disabled}
                  onClick={() => createAndRun.mutate()}
                >
                  {createAndRun.isPending ? "Starting…" : "Create & run"}
                </Button>
              </div>
            )
          })()}
        </div>
      </DialogContent>
    </Dialog>
  )
}
