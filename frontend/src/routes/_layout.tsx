import { createFileRoute, Outlet, redirect } from "@tanstack/react-router"

import { UsersService } from "@/client"
import { Footer } from "@/components/Common/Footer"
import AppSidebar from "@/components/Sidebar/AppSidebar"
import {
  SidebarInset,
  SidebarProvider,
  SidebarTrigger,
} from "@/components/ui/sidebar"
import { clearRejectedToken, isLoggedIn } from "@/hooks/useAuth"
import { CommandTrackingProvider } from "@/hooks/useCommandTracking"

export const Route = createFileRoute("/_layout")({
  component: Layout,
  beforeLoad: async () => {
    if (!isLoggedIn()) {
      throw redirect({
        to: "/login",
      })
    }
    try {
      // A present token is not a valid session. Checking here, once for the
      // whole shell, means the pages below never run with a token the API
      // rejects — they would otherwise throw out of their own beforeLoad and
      // render an error instead of asking for a fresh login.
      await UsersService.readUserMe()
    } catch (error) {
      if (clearRejectedToken(error)) {
        throw redirect({
          to: "/login",
        })
      }
      throw error
    }
  },
})

function Layout() {
  return (
    <CommandTrackingProvider>
      <SidebarProvider>
        <AppSidebar />
        <SidebarInset>
          <header className="sticky top-0 z-10 flex h-16 shrink-0 items-center gap-2 border-b bg-background px-4">
            <SidebarTrigger className="-ml-1 text-muted-foreground" />
          </header>
          <main className="flex-1 p-6 md:p-8">
            <div className="mx-auto max-w-7xl">
              <Outlet />
            </div>
          </main>
          <Footer />
        </SidebarInset>
      </SidebarProvider>
    </CommandTrackingProvider>
  )
}
