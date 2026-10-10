// Typed API client mirroring backend/tamam/api/routes.py
import { getToken } from "@/lib/auth"

export type Lifecycle = "queued" | "starting" | "running" | "judging" | "finished"
export type Outcome = "passed" | "failed" | "error" | null

export interface Project {
  id: string
  name: string
  description: string
  base_url: string
  created_at: string
}

// Check semantics live in the backend registry; the FE only renders descriptors.
export interface Check {
  kind: string
  value?: string | null
}

export interface CheckDescriptor {
  kind: string
  label: string
  description: string
  requires_value: boolean
  placeholder: string | null
}

export interface Test {
  id: string
  project_id: string
  name: string
  instructions: string
  start_url: string
  checks: Check[]
  checks_exhaustive: boolean
  executor_id: string | null
  build_id?: string | null
  app_build_ref?: string | null
  app_bundle_id?: string | null
  device?: string | null
  ios_platform_version?: string | null
  ios_target?: string | null
  reuse_session?: boolean
  created_at: string
}

export interface IosDevice {
  device: string
  versions: string[]
}

export interface Build {
  id: string
  project_id: string
  name: string
  app_path: string
  bundle_id: string | null
  created_at: string
}

export interface Verdict {
  passed: boolean
  reason: string
  reasoning?: string
  repro_steps?: string[]
  confidence?: number | null
}

/** One checklist clause and how it was verified. `how` is set on fast-replay runs. */
export interface RunClause {
  id: string
  text: string
  status: "pending" | "done" | "failed"
  step: number | null
  evidence: string
  how?: "code" | "ai" | "replayed"
}

export interface ReplayCheck {
  id: string
  clause: string
  how: "code" | "ai"
  ok: boolean
  detail?: string
}

/** What fast mode did on this run (see docs/guide/test-graduation.md). */
export interface RunGraduation {
  mode?: "fast" | "probation" | "fallback" | "handoff"
  replay?: {
    verdict?: "passed" | "failed" | "miss" | "error"
    reason?: string
    seconds?: number | null
    checks?: ReplayCheck[] | null
    at_step?: string | null
    steps_done?: number | null
    seen?: string[] | null
    ai_looks?: number | null
  }
  proven_by_replay?: string[]
  script_version?: number
  script_status?: "probation" | "active" | "retired" | "superseded"
  agree?: boolean | null
  agree_elsewhere?: boolean
  agree_in_a_row?: number
  promote_after?: number
  promoted?: boolean
  stays_in_probation?: string
  retired?: string
  // Compile decision after an AI run
  ready?: boolean
  reasons?: string[]
  summary?: string
  version?: number
}

export interface TestRun {
  id: string
  test_id: string
  lifecycle: Lifecycle
  outcome: Outcome
  status_message: string
  judge_verdict: Verdict | null
  self_verdict: Verdict | null
  disputed: boolean
  /** Frozen at run start. verdict_source: who decided what the checks left open. */
  spec_snapshot?: { verdict_source?: "agent" | "judge" }
  video_url: string | null
  live_url: string | null
  usage: {
    total_tokens?: number
    total_cost?: number
    duration_s?: number
    judge_tokens?: number
    timings?: { queue_wait_s?: number; execute_s?: number; verify_s?: number }
  }
  executor_id: string
  clauses?: RunClause[] | null
  graduation?: RunGraduation | null
  created_at: string
  started_at: string | null
  finished_at: string | null
}

export interface StepRow {
  id: number
  run_id: string
  step: number
  payload: {
    url?: string
    title?: string
    memory?: string
    next_goal?: string
    actions?: Record<string, unknown>[]
    duration_s?: number
  }
  screenshot_url: string | null
  annotated_url?: string | null   // set-of-marks image the agent actually saw
  created_at: string
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { ...(init?.headers as Record<string, string>) }
  // FormData (multipart) sets its own content-type with a boundary — don't override.
  if (!(init?.body instanceof FormData) && !headers["content-type"]) {
    headers["content-type"] = "application/json"
  }
  const token = await getToken()
  if (token) headers["authorization"] = `Bearer ${token}`
  const response = await fetch(`/api${path}`, { ...init, headers })
  if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`)
  if (response.status === 204) return undefined as T
  return response.json()
}

export const api = {
  checks: {
    kinds: () => request<CheckDescriptor[]>("/check-kinds"),
  },
  executors: {
    list: () => request<string[]>("/executors"),
  },
  ios: {
    // Real cloud iOS devices ({device, versions[]}, newest-OS-first) for the picker.
    devices: () => request<IosDevice[]>("/ios/devices"),
  },
  projects: {
    list: () => request<Project[]>("/projects"),
    get: (id: string) => request<Project>(`/projects/${id}`),
    create: (body: Partial<Project>) =>
      request<Project>("/projects", { method: "POST", body: JSON.stringify(body) }),
    runs: (id: string) => request<TestRun[]>(`/projects/${id}/runs`),
    tests: (id: string) => request<Test[]>(`/projects/${id}/tests`),
    builds: (id: string) => request<Build[]>(`/projects/${id}/builds`),
  },
  builds: {
    create: (projectId: string, body: { name: string; app_path: string; bundle_id?: string | null }) =>
      request<Build>(`/projects/${projectId}/builds`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    upload: async (
      projectId: string,
      { file, name, bundle_id }: { file: File; name: string; bundle_id?: string | null },
    ) => {
      const fd = new FormData()
      fd.append("file", file)
      fd.append("name", name)
      if (bundle_id) fd.append("bundle_id", bundle_id)
      const token = await getToken()
      // No content-type header — the browser sets the multipart boundary itself.
      const res = await fetch(`/api/projects/${projectId}/builds/upload`, {
        method: "POST",
        body: fd,
        headers: token ? { authorization: `Bearer ${token}` } : undefined,
      })
      if (!res.ok) throw new Error(`${res.status}: ${await res.text()}`)
      return (await res.json()) as Build
    },
    remove: (id: string) => request<void>(`/builds/${id}`, { method: "DELETE" }),
  },
  tests: {
    get: (id: string) => request<Test>(`/tests/${id}`),
    create: (projectId: string, body: Partial<Test>) =>
      request<Test>(`/projects/${projectId}/tests`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    update: (id: string, body: Partial<Test>) =>
      request<Test>(`/tests/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
    remove: (id: string) => request<void>(`/tests/${id}`, { method: "DELETE" }),
    run: (id: string) => request<TestRun>(`/tests/${id}/runs`, { method: "POST" }),
    runs: (id: string) => request<TestRun[]>(`/tests/${id}/runs`),
  },
  runs: {
    get: (id: string) => request<{ run: TestRun; steps: StepRow[] }>(`/runs/${id}`),
    // EventSource can't set headers, so the token rides as a query param.
    eventsUrl: async (id: string) => {
      const token = await getToken()
      return `/api/runs/${id}/events${token ? `?token=${encodeURIComponent(token)}` : ""}`
    },
  },
}
