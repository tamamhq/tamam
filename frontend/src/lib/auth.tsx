/**
 * Auth seam — the ONLY frontend file that imports Clerk.
 *
 * The rest of the app imports auth from here (AuthProvider, AuthGate, UserMenu,
 * getToken), never from @clerk/clerk-react. Swapping providers = rewriting this
 * one file. Same "own the loop, isolate the vendor" pattern as the backend.
 */
import { OrganizationSwitcher, SignIn, SignedIn, SignedOut, UserButton, ClerkProvider, useAuth } from "@clerk/clerk-react"
import type { ReactNode } from "react"

const PUBLISHABLE_KEY = import.meta.env.VITE_CLERK_PUBLISHABLE_KEY as string | undefined

/** Wrap the app so Clerk is available. */
export function AuthProvider({ children }: { children: ReactNode }) {
  if (!PUBLISHABLE_KEY) {
    // No key configured (e.g. a build without auth) — render app as-is.
    return <>{children}</>
  }
  return (
    <ClerkProvider
      publishableKey={PUBLISHABLE_KEY}
      // After sign-in/up (incl. the OAuth round-trip), come back to the app — not
      // Clerk's hosted Account Portal.
      signInFallbackRedirectUrl="/"
      signUpFallbackRedirectUrl="/"
      afterSignOutUrl="/"
    >
      {children}
    </ClerkProvider>
  )
}

/** Signed-out users see a centered sign-in card; signed-in users see the app. */
export function AuthGate({ children }: { children: ReactNode }) {
  if (!PUBLISHABLE_KEY) return <>{children}</> // auth disabled build
  return (
    <>
      <SignedIn>{children}</SignedIn>
      <SignedOut>
        <div className="flex min-h-screen items-center justify-center bg-background p-6">
          <SignIn routing="hash" fallbackRedirectUrl="/" signUpUrl="/" />
        </div>
      </SignedOut>
    </>
  )
}

/** Org switcher — full width for the sidebar footer. */
export function OrgSwitcher() {
  if (!PUBLISHABLE_KEY) return null
  return (
    <OrganizationSwitcher
      hidePersonal={false}
      afterSelectOrganizationUrl="/"
      appearance={{ elements: { rootBox: "w-full", organizationSwitcherTrigger: "w-full justify-start" } }}
    />
  )
}

/** User avatar + menu. */
export function UserAvatar() {
  if (!PUBLISHABLE_KEY) return null
  return <UserButton afterSignOutUrl="/" />
}

/**
 * Fetch a fresh session token for API calls. Non-React code (our api client) can't
 * use hooks, so we read Clerk off the window — but only from THIS seam file, so the
 * coupling stays contained. Returns null when auth is disabled.
 */
export async function getToken(): Promise<string | null> {
  if (!PUBLISHABLE_KEY) return null
  const clerk = (window as unknown as { Clerk?: { session?: { getToken: () => Promise<string | null> } } }).Clerk
  try {
    return (await clerk?.session?.getToken()) ?? null
  } catch {
    return null
  }
}

// Re-export the hook for components that want live auth state.
export { useAuth }
