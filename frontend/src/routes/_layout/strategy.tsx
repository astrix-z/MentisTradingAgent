import { useMutation, useQuery } from "@tanstack/react-query"
import { createFileRoute, redirect } from "@tanstack/react-router"
import { Play, Square } from "lucide-react"
import { useEffect, useState } from "react"

import { type StartPayload, TradingService, UsersService } from "@/client"
import { CommandTrackingView } from "@/components/CommandTrackingView"
import { TradingStatusView } from "@/components/TradingStatusView"
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
import { useCommandTracking } from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"

type Network = NonNullable<StartPayload["network"]>

const STRATEGY = "tripple_screen"

// Triple Screen defaults (must match agent/agent/strategies/tripple_screen/settings.py).
const DEFAULT_PARAMS = {
  quote_asset: "USDT",
  tide_interval: "4h",
  wave_interval: "1h",
  ripple_interval: "15m",
  ema_tide_periods: [5, 13, 34],
  stochastic_k_period: 5,
  stochastic_d_period: 3,
  stochastic_slow_period: 3,
  fi_ema_period: 2,
  oversold_threshold: 20,
  swing_low_h1_n: 2,
  trail_m15_n: 2,
  stop_buffer_pct: 0.1,
  min_stop_distance_pct: 1,
  tp_pct: 10,
  cooldown_minutes: 60,
  signal_validity_minutes: 15,
  rest_concurrency: 3,
  ws_top_n: 50,
  volume_window: "24h",
  kline_window: 1000,
} satisfies Record<string, string | number | number[]>

type ParamKey = keyof typeof DEFAULT_PARAMS
type FieldType = "string" | "number" | "numberArray"

type FieldDef = {
  key: ParamKey
  label: string
  type: FieldType
}

const FIELDS: FieldDef[] = [
  { key: "quote_asset", label: "Quote asset", type: "string" },
  { key: "tide_interval", label: "Tide interval", type: "string" },
  { key: "wave_interval", label: "Wave interval", type: "string" },
  { key: "ripple_interval", label: "Ripple interval", type: "string" },
  { key: "ema_tide_periods", label: "EMA tide periods", type: "numberArray" },
  { key: "stochastic_k_period", label: "Stochastic %K period", type: "number" },
  { key: "stochastic_d_period", label: "Stochastic %D period", type: "number" },
  {
    key: "stochastic_slow_period",
    label: "Stochastic slow period",
    type: "number",
  },
  { key: "fi_ema_period", label: "Force index EMA period", type: "number" },
  { key: "oversold_threshold", label: "Oversold threshold", type: "number" },
  { key: "swing_low_h1_n", label: "Swing low H1 N", type: "number" },
  { key: "trail_m15_n", label: "Trail M15 N", type: "number" },
  { key: "stop_buffer_pct", label: "Stop buffer %", type: "number" },
  {
    key: "min_stop_distance_pct",
    label: "Min stop distance %",
    type: "number",
  },
  { key: "tp_pct", label: "Take profit %", type: "number" },
  { key: "cooldown_minutes", label: "Cooldown (minutes)", type: "number" },
  {
    key: "signal_validity_minutes",
    label: "Signal validity (minutes)",
    type: "number",
  },
  { key: "rest_concurrency", label: "REST concurrency", type: "number" },
  { key: "ws_top_n", label: "WS top N symbols", type: "number" },
  { key: "volume_window", label: "Volume window", type: "string" },
  { key: "kline_window", label: "Kline window", type: "number" },
]

const initialValues = (): Record<ParamKey, string> => {
  const values = {} as Record<ParamKey, string>
  for (const field of FIELDS) {
    const value = DEFAULT_PARAMS[field.key]
    values[field.key] = Array.isArray(value) ? value.join(", ") : String(value)
  }
  return values
}

const tradingStatusQueryOptions = () => ({
  queryFn: async () => (await TradingService.getStatus()).data,
  queryKey: ["trading-status"],
  refetchInterval: 5000,
})

export const Route = createFileRoute("/_layout/strategy")({
  component: Strategy,
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
        title: "Strategy - FastAPI Template",
      },
    ],
  }),
})

function Strategy() {
  const { showErrorToast } = useCustomToast()
  const { commands, track } = useCommandTracking()
  const { data: status, isError } = useQuery(tradingStatusQueryOptions())

  const [network, setNetwork] = useState<Network>("testnet")
  const [symbolsInput, setSymbolsInput] = useState("")
  const [values, setValues] = useState<Record<ParamKey, string>>(initialValues)

  const [dirty, setDirty] = useState(false)
  const [strategy, setStrategy] = useState(STRATEGY)
  const [savedParams, setSavedParams] = useState<Record<string, unknown>>({})
  useEffect(() => {
    if (dirty || !status) return
    const snapshot = status.desired
    const next = initialValues()
    for (const field of FIELDS) {
      const value = snapshot.params?.[field.key]
      if (value !== undefined)
        next[field.key] = Array.isArray(value)
          ? value.join(", ")
          : String(value)
    }
    setValues(next)
    setNetwork(snapshot.network ?? "testnet")
    setStrategy(snapshot.strategy ?? STRATEGY)
    setSymbolsInput((snapshot.symbols ?? []).join(", "))
    setSavedParams(snapshot.params ?? {})
  }, [status, dirty])

  const parsedSymbols = Array.from(
    new Set(
      symbolsInput
        .split(",")
        .map((symbol) => symbol.trim().toUpperCase())
        .filter(Boolean),
    ),
  )

  const startMutation = useMutation({
    mutationFn: (body: StartPayload) => TradingService.startTrading({ body }),
    onSuccess: ({ data }) => track(data),
    onError: handleError.bind(showErrorToast),
  })
  const stopMutation = useMutation({
    mutationFn: () => TradingService.stopTrading(),
    onSuccess: ({ data }) => track(data),
    onError: handleError.bind(showErrorToast),
  })

  const pending = startMutation.isPending || stopMutation.isPending

  const setValue = (key: ParamKey, value: string) => {
    setDirty(true)
    setValues((current) => ({ ...current, [key]: value }))
  }

  const buildParams = (): Record<string, unknown> | null => {
    const params: Record<string, unknown> = { ...savedParams }
    for (const field of FIELDS) {
      const raw = values[field.key].trim()
      if (field.type === "number") {
        const parsed = Number(raw)
        if (raw === "" || Number.isNaN(parsed)) {
          showErrorToast(`${field.label} must be a number`)
          return null
        }
        params[field.key] = parsed
      } else if (field.type === "numberArray") {
        const parsed = raw
          .split(",")
          .map((part) => Number(part.trim()))
          .filter((part) => !Number.isNaN(part))
        if (parsed.length === 0) {
          showErrorToast(`${field.label} must be a list of numbers`)
          return null
        }
        params[field.key] = parsed
      } else {
        if (raw === "") {
          showErrorToast(`${field.label} is required`)
          return null
        }
        params[field.key] = raw
      }
    }
    return params
  }

  const onStart = () => {
    if (parsedSymbols.length === 0) {
      showErrorToast("Enter at least one symbol to trade (e.g. BTCUSDT)")
      return
    }
    const params = buildParams()
    if (params === null) return
    startMutation.mutate({
      network,
      strategy,
      symbols: parsedSymbols,
      params,
    })
  }

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Strategy</h1>
        <p className="text-muted-foreground">
          Configure the Triple Screen strategy and start or stop the agent
        </p>
      </div>

      <CommandTrackingView commands={commands} />
      <p>
        {dirty
          ? "Unsaved local draft — background updates will not overwrite it"
          : "Saved desired configuration"}
      </p>
      {dirty && (
        <Button variant="outline" onClick={() => setDirty(false)}>
          Reload desired configuration
        </Button>
      )}
      {strategy !== STRATEGY && (
        <p role="alert">
          Saved strategy {strategy} is not supported by this form. Start
          disabled.
        </p>
      )}
      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Status</CardTitle>
            <CardDescription>Agent liveness and current state</CardDescription>
          </CardHeader>
          <CardContent>
            <TradingStatusView status={status} error={isError} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Control</CardTitle>
            <CardDescription>
              Start the strategy with the settings below, or stop the agent
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-6">
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
                      aria-pressed={active}
                      onClick={() => {
                        setDirty(true)
                        setNetwork(option)
                      }}
                    >
                      {option.toUpperCase()}
                    </Button>
                  )
                })}
              </div>
            </div>

            <div className="flex flex-col gap-2">
              <Label htmlFor="symbols">Symbols</Label>
              <Input
                id="symbols"
                placeholder="e.g. BTCUSDT, ETHUSDT"
                value={symbolsInput}
                disabled={pending}
                onChange={(event) => {
                  setDirty(true)
                  setSymbolsInput(event.target.value)
                }}
              />
              <span className="text-muted-foreground text-xs">
                Comma-separated, uppercased and de-duplicated automatically
              </span>
            </div>

            <div className="flex gap-2">
              <Button
                type="button"
                disabled={pending || !status || strategy !== STRATEGY}
                onClick={onStart}
                className="flex-1"
              >
                <Play className="mr-1 size-4" />
                {startMutation.isPending ? "Starting…" : "Start"}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={pending}
                onClick={() => stopMutation.mutate()}
                className="flex-1"
              >
                <Square className="mr-1 size-4" />
                {stopMutation.isPending ? "Stopping…" : "Stop"}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Triple Screen settings</CardTitle>
          <CardDescription>
            Fixed at start; the agent validates them before the strategy runs
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {FIELDS.map((field) => (
              <div key={field.key} className="flex flex-col gap-2">
                <Label htmlFor={field.key}>{field.label}</Label>
                <Input
                  id={field.key}
                  value={values[field.key]}
                  disabled={pending}
                  inputMode={field.type === "string" ? undefined : "decimal"}
                  onChange={(event) => setValue(field.key, event.target.value)}
                />
              </div>
            ))}
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
