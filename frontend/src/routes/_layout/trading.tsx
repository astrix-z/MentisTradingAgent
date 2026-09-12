import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, redirect } from "@tanstack/react-router"
import { CircleCheck, CircleX } from "lucide-react"
import { useEffect, useRef, useState } from "react"

import { type StartPayload, TradingService, UsersService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"

type Network = NonNullable<StartPayload["network"]>

const tradingStatusQueryOptions = () => ({
  queryFn: async () => (await TradingService.getStatus()).data,
  queryKey: ["trading-status"],
  refetchInterval: 5000,
})

const tradingResultQueryOptions = () => ({
  queryFn: async () => (await TradingService.getCommandResult()).data,
  queryKey: ["trading-result"],
})

export const Route = createFileRoute("/_layout/trading")({
  component: Trading,
  beforeLoad: async () => {
    const { data: user } = await UsersService.readUserMe()
    if (!user.is_superuser) {
      throw redirect({
        to: "/",
      })
    }
  },
  head: () => ({
    meta: [
      {
        title: "Trading - FastAPI Template",
      },
    ],
  }),
})

function Trading() {
  const queryClient = useQueryClient()
  const { showSuccessToast, showErrorToast } = useCustomToast()

  const { data: status, isLoading } = useQuery(tradingStatusQueryOptions())
  const runningIntent = status?.running ?? false

  // Start parameters (network + strategy + symbols) are edited locally and
  // sent along with the start command. Seed them once from the server snapshot.
  const [network, setNetwork] = useState<Network>("testnet")
  const [strategy, setStrategy] = useState("")
  const [symbolsInput, setSymbolsInput] = useState("")
  const seeded = useRef(false)
  useEffect(() => {
    if (!seeded.current && status) {
      setNetwork(status.network ?? "testnet")
      setStrategy(status.strategy ?? "")
      setSymbolsInput((status.symbols ?? []).join(", "))
      seeded.current = true
    }
  }, [status])

  // Command result: show the latest result and poll briefly after issuing a
  // command, since the agent produces the result asynchronously.
  const { data: commandResult } = useQuery(tradingResultQueryOptions())
  const [requestTick, setRequestTick] = useState(0)
  const balanceMutation = useMutation({
    mutationFn: (net: Network) =>
      TradingService.fetchBalance({ body: { network: net } }),
    onSuccess: () => {
      showSuccessToast("Balance requested")
      setRequestTick((tick) => tick + 1)
    },
    onError: handleError.bind(showErrorToast),
  })
  useEffect(() => {
    if (requestTick === 0) return
    const poll = setInterval(() => {
      queryClient.invalidateQueries({ queryKey: ["trading-result"] })
    }, 1500)
    const done = setTimeout(() => clearInterval(poll), 7000)
    return () => {
      clearInterval(poll)
      clearTimeout(done)
    }
  }, [requestTick, queryClient])

  // Selectable strategies come from the agent (via the backend).
  const { data: strategies } = useQuery({
    queryKey: ["trading-strategies"],
    queryFn: async () => (await TradingService.listStrategies()).data,
  })
  const strategyOptions = strategies ?? []

  const parsedSymbols = symbolsInput
    .split(",")
    .map((symbol) => symbol.trim().toUpperCase())
    .filter(Boolean)

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["trading-status"] })

  const startMutation = useMutation({
    mutationFn: (body: StartPayload) => TradingService.startTrading({ body }),
    onSuccess: () => {
      showSuccessToast("Trading started")
      invalidate()
    },
    onError: handleError.bind(showErrorToast),
  })
  const stopMutation = useMutation({
    mutationFn: () => TradingService.stopTrading(),
    onSuccess: () => {
      showSuccessToast("Trading stopped")
      invalidate()
    },
    onError: handleError.bind(showErrorToast),
  })

  const pending = startMutation.isPending || stopMutation.isPending
  const trimmedStrategy = strategy.trim()

  const startWith = (nextNetwork: Network, nextStrategy: string) => {
    const name = nextStrategy.trim()
    if (!name) {
      showErrorToast("Strategy name is required to start trading")
      return
    }
    if (parsedSymbols.length === 0) {
      showErrorToast("Enter at least one symbol to trade (e.g. BTCUSDT)")
      return
    }
    startMutation.mutate({
      network: nextNetwork,
      strategy: name,
      symbols: parsedSymbols,
    })
  }

  const onRunningChange = (checked: boolean) => {
    if (checked) {
      startWith(network, trimmedStrategy)
    } else {
      stopMutation.mutate()
    }
  }

  const onNetworkSelect = (option: Network) => {
    setNetwork(option)
    // If the engine is already running, restart it with the new network.
    if (runningIntent) {
      startWith(option, trimmedStrategy)
    }
  }

  const onStrategyApply = () => {
    if (runningIntent && strategy !== status?.strategy) {
      startWith(network, trimmedStrategy)
    }
  }

  const onSymbolsApply = () => {
    const current = (status?.symbols ?? []).join(",")
    if (runningIntent && parsedSymbols.join(",") !== current) {
      startWith(network, trimmedStrategy)
    }
  }

  const online = status?.online ?? false
  const agentState = status?.state ?? null

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Trading</h1>
        <p className="text-muted-foreground">
          Send commands to the trading agent and pick its network and strategy
        </p>
      </div>

      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Control</CardTitle>
            <CardDescription>
              Commands are sent to the agent immediately
            </CardDescription>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <p className="text-muted-foreground text-sm">Loading…</p>
            ) : (
              <div className="flex flex-col gap-6">
                <div className="flex items-center justify-between gap-4">
                  <div className="flex flex-col gap-1">
                    <Label htmlFor="run">Running</Label>
                    <span className="text-muted-foreground text-sm">
                      {runningIntent
                        ? "Engine is running"
                        : "Engine is stopped"}
                    </span>
                  </div>
                  <Switch
                    id="run"
                    checked={runningIntent}
                    disabled={pending}
                    onCheckedChange={onRunningChange}
                  />
                </div>

                <div className="flex flex-col gap-2">
                  <Label>Network</Label>
                  <div className="flex w-fit overflow-hidden rounded-md border">
                    {(["testnet", "live"] as const).map((option) => {
                      const active = network === option
                      return (
                        <Button
                          key={option}
                          type="button"
                          size="sm"
                          variant={active ? "default" : "ghost"}
                          disabled={pending}
                          className={cn(
                            "rounded-none",
                            active ? "font-semibold" : "text-muted-foreground",
                          )}
                          onClick={() => onNetworkSelect(option)}
                        >
                          {option.toUpperCase()}
                        </Button>
                      )
                    })}
                  </div>
                </div>

                <div className="flex flex-col gap-2">
                  <Label htmlFor="strategy">Strategy name</Label>
                  <Input
                    id="strategy"
                    list="strategies-list"
                    placeholder="e.g. grid"
                    value={strategy}
                    disabled={pending}
                    onChange={(event) => setStrategy(event.target.value)}
                    onBlur={onStrategyApply}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.currentTarget.blur()
                      }
                    }}
                  />
                  <datalist id="strategies-list">
                    {strategyOptions.map((option) => (
                      <option key={option} value={option} />
                    ))}
                  </datalist>
                </div>

                <div className="flex flex-col gap-2">
                  <Label htmlFor="symbols">Symbols</Label>
                  <Input
                    id="symbols"
                    placeholder="e.g. BTCUSDT, ETHUSDT"
                    value={symbolsInput}
                    disabled={pending}
                    onChange={(event) => setSymbolsInput(event.target.value)}
                    onBlur={onSymbolsApply}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.currentTarget.blur()
                      }
                    }}
                  />
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Status</CardTitle>
            <CardDescription>Agent liveness and current state</CardDescription>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <p className="text-muted-foreground text-sm">Loading…</p>
            ) : (
              <div className="flex flex-col gap-4 text-sm">
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Agent</span>
                  {online ? (
                    <Badge
                      variant="default"
                      className="bg-emerald-600 text-white"
                    >
                      <CircleCheck className="mr-1 size-3.5" />
                      online
                    </Badge>
                  ) : (
                    <Badge variant="destructive">
                      <CircleX className="mr-1 size-3.5" />
                      offline
                    </Badge>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">State</span>
                  <span className="font-medium capitalize">
                    {agentState ?? "unknown"}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Running</span>
                  <span className="font-medium">
                    {runningIntent ? "on" : "off"}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Network</span>
                  <span className="font-medium uppercase">
                    {status?.network ?? "—"}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Strategy</span>
                  <span className="font-medium">{status?.strategy ?? "—"}</span>
                </div>
                {(status?.symbols?.length ?? 0) > 0 && (
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground">Symbols</span>
                    <span className="truncate text-right font-medium">
                      {status?.symbols?.join(", ")}
                    </span>
                  </div>
                )}
                {status?.message && (
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground">Message</span>
                    <span className="truncate text-right font-medium">
                      {status.message}
                    </span>
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Command result</CardTitle>
          <CardDescription>
            The outcome of the last command sent to the agent
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col gap-4">
            <div className="flex items-center justify-between gap-4">
              <div>
                <p className="text-muted-foreground text-sm">
                  Use the balance command to fetch the account balance on demand
                </p>
              </div>
              <Button
                type="button"
                disabled={balanceMutation.isPending}
                onClick={() => balanceMutation.mutate(network)}
              >
                {balanceMutation.isPending ? "Requesting…" : "Check balance"}
              </Button>
            </div>

            {commandResult ? (
              <div className="flex flex-col gap-3 text-sm">
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Command</span>
                  <span className="font-medium capitalize">
                    {commandResult.type}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">Status</span>
                  {commandResult.ok ? (
                    <Badge
                      variant="default"
                      className="bg-emerald-600 text-white"
                    >
                      <CircleCheck className="mr-1 size-3.5" />
                      ok
                    </Badge>
                  ) : (
                    <Badge variant="destructive">
                      <CircleX className="mr-1 size-3.5" />
                      failed
                    </Badge>
                  )}
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-muted-foreground">At</span>
                  <span className="font-medium">
                    {new Date(commandResult.at).toLocaleString()}
                  </span>
                </div>
                {commandResult.message && (
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-muted-foreground">Message</span>
                    <span className="text-right font-medium">
                      {commandResult.message}
                    </span>
                  </div>
                )}
                {commandResult.ok &&
                  commandResult.data &&
                  typeof commandResult.data === "object" &&
                  !Array.isArray(commandResult.data) &&
                  Object.keys(commandResult.data).length > 0 && (
                    <div className="border-t pt-3">
                      <span className="text-muted-foreground">Data</span>
                      <div className="mt-2 grid grid-cols-1 gap-1">
                        {Object.entries(
                          commandResult.data as Record<string, unknown>,
                        ).map(([key, value]) => (
                          <div
                            key={key}
                            className="flex items-center justify-between gap-2"
                          >
                            <span className="font-medium">{key}</span>
                            <span className="tabular-nums">
                              {String(value)}
                            </span>
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
              </div>
            ) : (
              <p className="text-muted-foreground text-sm">
                No command result yet — send a command and it will appear here.
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
