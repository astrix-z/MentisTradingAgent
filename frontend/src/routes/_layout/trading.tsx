import { useMutation, useQuery } from "@tanstack/react-query"
import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import { useState } from "react"

import { type NetworkRequest, TradingService, UsersService } from "@/client"
import { CommandTrackingView } from "@/components/CommandTrackingView"
import { TradingStatusView } from "@/components/TradingStatusView"
import { Button } from "@/components/ui/button"
import { useCommandTracking } from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/trading")({
  component: Trading,
  beforeLoad: async () => {
    const { data: user } = await UsersService.readUserMe()
    if (!user.is_superuser) throw redirect({ to: "/" })
  },
  head: () => ({ meta: [{ title: "Trading - FastAPI Template" }] }),
})

function Trading() {
  const { data: status, isError } = useQuery({
    queryFn: async () => (await TradingService.getStatus()).data,
    queryKey: ["trading-status"],
    refetchInterval: 5000,
  })
  const [network, setNetwork] = useState<NetworkRequest["network"]>()
  const { commands, track } = useCommandTracking()
  const { showErrorToast } = useCustomToast()
  const balanceMutation = useMutation({
    mutationFn: (network: NonNullable<NetworkRequest["network"]>) =>
      TradingService.fetchBalance({ body: { network } }),
    onSuccess: ({ data }) => track(data),
    onError: handleError.bind(showErrorToast),
  })

  return (
    <div className="flex flex-col gap-6">
      <h1 className="text-2xl font-bold tracking-tight">Trading</h1>
      <Link to="/strategy" className="underline">
        Configure strategy / Start / Stop
      </Link>
      <TradingStatusView status={status} error={isError} />
      <section className="space-y-3" aria-label="Balance observation">
        <h2 className="font-semibold">Check balance</h2>
        <p>
          Select a balance network explicitly. This does not change the strategy
          configuration.
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
        <CommandTrackingView commands={commands} />
      </section>
    </div>
  )
}
