import { createFileRoute, redirect } from "@tanstack/react-router"

// Legacy route: the balance flow now lives on its own page.
export const Route = createFileRoute("/_layout/trading")({
  beforeLoad: () => {
    throw redirect({ to: "/balance" })
  },
})
