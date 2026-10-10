import { Link, Outlet, useLocation } from "react-router-dom"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarHeader,
  SidebarInset,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { Separator } from "@/components/ui/separator"
import { OrgSwitcher, UserAvatar } from "@/lib/auth"
import { useTheme } from "@/lib/theme"
import { FolderGit2, Moon, Settings, Sun, Zap } from "lucide-react"

const nav = [
  { title: "Projects", url: "/", icon: FolderGit2, match: (p: string) => p === "/" || p.startsWith("/projects") },
  { title: "Runs", url: "/runs", icon: Zap, match: (p: string) => p.startsWith("/runs") },
  { title: "Settings", url: "/settings", icon: Settings, match: (p: string) => p.startsWith("/settings") },
]

function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const isDark = document.documentElement.classList.contains("dark")
  return (
    <button
      onClick={() => setTheme(isDark ? "light" : "dark")}
      className="flex size-8 items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-sidebar-accent hover:text-foreground"
      title={`Theme: ${theme}`}
    >
      {isDark ? <Sun className="size-4" /> : <Moon className="size-4" />}
    </button>
  )
}

export function Layout() {
  const { pathname } = useLocation()
  return (
    <SidebarProvider>
      <Sidebar collapsible="icon">
        <SidebarHeader>
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton size="lg" asChild>
                <Link to="/">
                  <img src="/favicon.svg" alt="" className="aspect-square size-8 shrink-0" />
                  <div className="grid flex-1 text-left leading-tight">
                    <span className="truncate font-semibold">Tamam</span>
                    <span className="truncate text-xs text-muted-foreground">iOS testing</span>
                  </div>
                </Link>
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarHeader>
        <SidebarContent>
          <SidebarGroup>
            <SidebarGroupContent>
              <SidebarMenu>
                {nav.map((item) => (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton asChild isActive={item.match(pathname)} tooltip={item.title}>
                      <Link to={item.url}>
                        <item.icon />
                        <span>{item.title}</span>
                      </Link>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>
        <SidebarFooter className="group-data-[collapsible=icon]:hidden">
          <div className="min-w-0 overflow-hidden rounded-md border border-sidebar-border bg-sidebar-accent/40 px-1 py-1">
            <OrgSwitcher />
          </div>
        </SidebarFooter>
      </Sidebar>
      <SidebarInset>
        <header className="flex h-14 shrink-0 items-center gap-2 border-b px-4">
          <SidebarTrigger className="-ml-1" />
          <Separator orientation="vertical" className="mr-2 h-4" />
          <Breadcrumbs pathname={pathname} />
          <div className="ml-auto flex items-center gap-1">
            <ThemeToggle />
            <UserAvatar />
          </div>
        </header>
        <main className="flex-1 p-4 md:p-6">
          <Outlet />
        </main>
      </SidebarInset>
    </SidebarProvider>
  )
}

function Breadcrumbs({ pathname }: { pathname: string }) {
  const label =
    pathname === "/"
      ? "Projects"
      : pathname === "/runs"
        ? "Runs"
        : pathname.startsWith("/runs")
          ? "Run"
          : pathname.startsWith("/tests")
            ? "Test"
            : pathname.startsWith("/settings")
              ? "Settings"
              : "Project"
  return <span className="text-sm font-medium text-muted-foreground">{label}</span>
}
