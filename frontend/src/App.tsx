import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { BrowserRouter, Route, Routes } from "react-router-dom"
import { AuthGate, AuthProvider } from "@/lib/auth"
import { ThemeProvider } from "@/lib/theme"
import { Layout } from "@/components/layout"
import { ProjectsPage } from "@/pages/projects"
import { ProjectDetailPage } from "@/pages/project-detail"
import { TestDetailPage } from "@/pages/test-detail"
import { RunDetailPage } from "@/pages/run-detail"
import { RunsPage } from "@/pages/runs"
import { SettingsPage } from "@/pages/settings"

const queryClient = new QueryClient()

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <AuthGate>
          <QueryClientProvider client={queryClient}>
            <BrowserRouter>
              <Routes>
                <Route element={<Layout />}>
                  <Route path="/" element={<ProjectsPage />} />
                  <Route path="/projects/:projectId" element={<ProjectDetailPage />} />
                  <Route path="/tests/:testId" element={<TestDetailPage />} />
                  <Route path="/runs" element={<RunsPage />} />
                  <Route path="/runs/:runId" element={<RunDetailPage />} />
                  <Route path="/settings" element={<SettingsPage />} />
                </Route>
              </Routes>
            </BrowserRouter>
          </QueryClientProvider>
        </AuthGate>
      </AuthProvider>
    </ThemeProvider>
  )
}
