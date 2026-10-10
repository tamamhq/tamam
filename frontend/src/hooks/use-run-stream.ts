import { useEffect, useState } from "react"
import type { Lifecycle, StepRow } from "@/lib/api"
import { api } from "@/lib/api"

interface StreamStep {
  step: number
  payload: StepRow["payload"]
  screenshot_url?: string | null
  annotated_url?: string | null
}

/** Subscribe to a run's SSE stream: replayed history + live steps + lifecycle +
 * live-preview frames (ephemeral CDP screencast, base64 JPEG). */
export function useRunStream(runId: string, active: boolean) {
  const [steps, setSteps] = useState<StreamStep[]>([])
  const [lifecycle, setLifecycle] = useState<Lifecycle | null>(null)
  const [frame, setFrame] = useState<string | null>(null)

  // A different run (e.g. a rerun navigating to a new id) must start blank — otherwise
  // the previous run's streamed steps/frame linger until a hard refresh.
  useEffect(() => {
    setSteps([])
    setLifecycle(null)
    setFrame(null)
  }, [runId])

  useEffect(() => {
    if (!active) return
    let source: EventSource | null = null
    let cancelled = false

    const wire = (s: EventSource) => {
      s.addEventListener("frame", (event) => {
        const data = JSON.parse((event as MessageEvent).data)
        if (data.data) setFrame(`data:image/jpeg;base64,${data.data}`)
      })
      s.addEventListener("step", (event) => {
        const data = JSON.parse((event as MessageEvent).data)
        const step: StreamStep = {
          step: data.step,
          payload: data.payload ?? {},
          screenshot_url: data.screenshot_url,
        }
        setSteps((prev) => {
          const others = prev.filter((x) => x.step !== step.step)
          return [...others, step].sort((a, b) => a.step - b.step)
        })
      })
      s.addEventListener("lifecycle", (event) => {
        const data = JSON.parse((event as MessageEvent).data)
        setLifecycle(data.lifecycle)
        if (data.lifecycle === "finished") s.close()
      })
      s.onerror = () => s.close()
    }

    // The events URL needs a fresh auth token, so it's async.
    void api.runs.eventsUrl(runId).then((url) => {
      if (cancelled) return
      source = new EventSource(url)
      wire(source)
    })

    return () => {
      cancelled = true
      source?.close()
    }
  }, [runId, active])

  return { steps, lifecycle, frame }
}
