import { useMutation, useQuery } from "@tanstack/react-query"
import { createFileRoute, redirect } from "@tanstack/react-router"
import { useState } from "react"

import { type NetworkRequest, TradingService, UsersService } from "@/client"
import { CommandTrackingView } from "@/components/CommandTrackingView"
import { TradingStatusView } from "@/components/TradingStatusView"
import { Button } from "@/components/ui/button"
import {
  latestTrackedCommand,
  useCommandTracking,
} from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/balance")({
  component: Balance,
  beforeLoad: async () => {
    const { data: user } = await UsersService.readUserMe()
    if (!user.is_superuser) throw redirect({ to: "/" })
  },
  head: () => ({ meta: [{ title: "Balance - FastAPI Template" }] }),
})

function Balance() {
  const { data: status, isError } = useQuery({
    queryFn: async () => (await TradingService.getStatus()).data,
    queryKey: ["trading-status"],
    refetchInterval: 5000,
  })
  const [network, setNetwork] = useState<NetworkRequest["network"]>()
  const { commands, track } = useCommandTracking()
  const { showErrorToast } = useCustomToast()
  // Only the newest request: a refresh replaces the previous section rather
  // than adding another one.
  const latestBalanceCommand = latestTrackedCommand(
    commands,
    (command) => command.envelope.type === "balance",
  )
  const balanceMutation = useMutation({
    mutationFn: (network: NonNullable<NetworkRequest["network"]>) =>
      TradingService.fetchBalance({ body: { network } }),
    onSuccess: ({ data }) => track(data),
    onError: handleError.bind(showErrorToast),
  })

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Balance</h1>
        <p className="text-muted-foreground">
          Request the account balance on a chosen network. This does not change
          the strategy configuration and does not start or stop trading.
        </p>
      </div>
      <TradingStatusView status={status} error={isError} balances />
      <section className="space-y-3" aria-label="Balance request">
        <h2 className="font-semibold">Check balance</h2>
        <p>
          The heartbeat balances shown above are an observed cache from the
          agent's last run and may be stale. Requesting a balance below asks the
          agent, right now, for a fresh snapshot on the network you select,
          correlated by its own command ID.
        </p>
        <div className="flex gap-2">
          {(["testnet", "live"] as const).map((option) => (
            <Button
              key={option}
              variant={network === option ? "default" : "outline"}
              aria-pressed={network === option}
              onClick={() => setNetwork(option)}
            >
              {option.toUpperCase()}
            </Button>
          ))}
          <Button
            disabled={!network || balanceMutation.isPending}
            onClick={() => network && balanceMutation.mutate(network)}
          >
            Check balance
          </Button>
        </div>
        <CommandTrackingView
          commands={latestBalanceCommand ? [latestBalanceCommand] : []}
        />
      </section>
    </div>
  )
}
