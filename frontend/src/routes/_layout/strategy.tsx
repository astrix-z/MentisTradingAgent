import { useMutation, useQuery } from "@tanstack/react-query"
import { createFileRoute, redirect } from "@tanstack/react-router"
import { Play, Square } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import { type StartPayload, TradingService, UsersService } from "@/client"
import { CommandTrackingView } from "@/components/CommandTrackingView"
import {
  type FieldValues,
  formatParamValue,
  parseSettingsSchema,
  seedFormValues,
  validateSettings,
} from "@/components/strategy-form/strategySchema"
import { TradingStatusView } from "@/components/TradingStatusView"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useCommandTracking } from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { handleError } from "@/utils"

type Network = NonNullable<StartPayload["network"]>
type ValidatedStart = {
  network: Network
  strategy: string
  symbols: string[]
  params: Record<string, unknown>
}

const tradingStatusQueryOptions = () => ({
  queryFn: async () => (await TradingService.getStatus()).data,
  queryKey: ["trading-status"],
  refetchInterval: 5000,
})

const strategyCatalogQueryOptions = () => ({
  queryFn: async () => (await TradingService.listStrategies()).data,
  queryKey: ["strategy-catalog"],
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

type ControlState =
  | "loading"
  | "timeout"
  | "awaiting-start"
  | "awaiting-stop"
  | "error"
  | "offline"
  | "running-confirmed"
  | "stopped-confirmed"
  | "unconfirmed"

function Strategy() {
  const { showErrorToast } = useCustomToast()
  const { commands, track } = useCommandTracking()
  const { data: status, isError } = useQuery(tradingStatusQueryOptions())
  const { data: catalog, isError: isCatalogError } = useQuery(
    strategyCatalogQueryOptions(),
  )

  const [network, setNetwork] = useState<Network>("testnet")
  const [symbolsInput, setSymbolsInput] = useState("")
  const [values, setValues] = useState<FieldValues>({})
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [formErrors, setFormErrors] = useState<string[]>([])
  const [symbolsError, setSymbolsError] = useState<string | null>(null)
  const [pendingLiveStart, setPendingLiveStart] =
    useState<ValidatedStart | null>(null)

  const [dirty, setDirty] = useState(false)
  const [strategy, setStrategy] = useState("")
  const [savedParams, setSavedParams] = useState<Record<string, unknown>>({})

  const strategies = catalog?.strategies
  const activeStrategy = useMemo(() => {
    const list = strategies ?? []
    if (list.length === 0) return undefined
    return list.find((item) => item.name === strategy) ?? list[0]
  }, [strategies, strategy])
  const fields = useMemo(
    () => parseSettingsSchema(activeStrategy?.settings_schema),
    [activeStrategy],
  )
  const offeredNames = useMemo(
    () => new Set((strategies ?? []).map((item) => item.name)),
    [strategies],
  )
  const activeLabel = activeStrategy?.label ?? "strategy"
  const catalogReady = !!catalog && catalog.available !== false
  const strategyOffered = strategy !== "" && offeredNames.has(strategy)

  useEffect(() => {
    if (dirty || !status || fields.length === 0) return
    const snapshot = status.desired
    setValues(seedFormValues(fields, snapshot.params))
    setNetwork(snapshot.network ?? "testnet")
    setStrategy(snapshot.strategy ?? "")
    setSymbolsInput((snapshot.symbols ?? []).join(", "))
    setSavedParams(snapshot.params ?? {})
  }, [status, fields, dirty])

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

  // The latest tracked start/stop command drives the "awaiting confirmation"
  // and "timeout" control states; a 202 Accepted is never treated as success.
  const latestLifecycleCommand = useMemo(() => {
    const lifecycle = commands.filter(
      (c) => c.envelope.type === "start" || c.envelope.type === "stop",
    )
    return lifecycle.length > 0 ? lifecycle[lifecycle.length - 1] : undefined
  }, [commands])

  const mismatch =
    !!status &&
    (!status.actual ||
      status.desired.running !== (status.actual.state === "running") ||
      (status.desired.running &&
        (status.desired.network !== status.actual.network ||
          status.desired.strategy !== status.actual.strategy)))

  const controlState: ControlState = useMemo(() => {
    if (!status) return "loading"
    if (latestLifecycleCommand?.phase === "timeout") return "timeout"
    if (
      latestLifecycleCommand &&
      (latestLifecycleCommand.phase === "accepted" ||
        latestLifecycleCommand.phase === "pending")
    ) {
      return latestLifecycleCommand.envelope.type === "start"
        ? "awaiting-start"
        : "awaiting-stop"
    }
    if (status.actual?.state === "error") return "error"
    if (!status.online) return "offline"
    if (
      status.desired.running &&
      status.actual?.state === "running" &&
      !mismatch
    )
      return "running-confirmed"
    if (!status.desired.running && status.actual?.state !== "running")
      return "stopped-confirmed"
    return "unconfirmed"
  }, [status, latestLifecycleCommand, mismatch])

  const CONTROL_STATE_COPY: Record<ControlState, string> = {
    loading: "Loading agent status…",
    timeout:
      "No confirmation for the last command within 120 seconds; outcome unknown.",
    "awaiting-start": "Start sent; awaiting agent confirmation.",
    "awaiting-stop": "Stop sent; awaiting agent confirmation.",
    error: "Agent reported an error state.",
    offline:
      "Agent offline / stale heartbeat; last known state may be outdated.",
    "running-confirmed": "Confirmed running with the desired configuration.",
    "stopped-confirmed": "Confirmed stopped.",
    unconfirmed: "Desired and actual state are not confirmed to match.",
  }

  // Once the draft is touched (dirty), Start is a deliberate reconfiguration,
  // not an unconscious repeat of the already-confirmed run. Start also needs a
  // published catalog and a strategy the agent actually offers.
  const startDisabled =
    pending ||
    !status ||
    !catalogReady ||
    !activeStrategy ||
    (strategy !== "" && !strategyOffered) ||
    controlState === "loading" ||
    (!dirty && controlState === "running-confirmed")
  const stopDisabled =
    pending ||
    controlState === "loading" ||
    controlState === "stopped-confirmed"

  const setValue = (key: string, value: string) => {
    setDirty(true)
    setValues((current) => ({ ...current, [key]: value }))
  }

  const runStart = (payload: ValidatedStart) => startMutation.mutate(payload)

  const onStart = () => {
    setSymbolsError(null)
    setFieldErrors({})
    setFormErrors([])
    if (!activeStrategy) return
    if (parsedSymbols.length === 0) {
      setSymbolsError("Enter at least one symbol to trade (e.g. BTCUSDT)")
      return
    }
    const result = validateSettings({
      fields,
      values,
      lookups: catalog?.lookups ?? {},
      constraints: activeStrategy.constraints ?? [],
      savedParams,
    })
    if (!result.ok) {
      setFieldErrors(result.fieldErrors)
      setFormErrors(result.formErrors)
      return
    }
    const payload: ValidatedStart = {
      network,
      strategy: strategy || activeStrategy.name,
      symbols: parsedSymbols,
      params: result.params,
    }
    if (network === "live") {
      setPendingLiveStart(payload)
      return
    }
    runStart(payload)
  }

  const hasErrors =
    Object.keys(fieldErrors).length > 0 ||
    formErrors.length > 0 ||
    !!symbolsError

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Strategy</h1>
        <p className="text-muted-foreground">
          Configure the {activeLabel} strategy and start or stop the agent
        </p>
      </div>

      <CommandTrackingView commands={commands} />
      <p role="status">{CONTROL_STATE_COPY[controlState]}</p>
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
      {strategy !== "" && !strategyOffered && (
        <p role="alert">
          Saved strategy {strategy} is not offered by the agent. Start disabled.
        </p>
      )}
      {hasErrors && (
        <div role="alert" className="space-y-1 rounded-md border p-3 text-sm">
          <p className="font-semibold">Fix the following before starting:</p>
          {symbolsError && <p>{symbolsError}</p>}
          {formErrors.map((message) => (
            <p key={message}>{message}</p>
          ))}
          {Object.entries(fieldErrors).map(([key, message]) => (
            <p key={key}>{message}</p>
          ))}
        </div>
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
                aria-invalid={!!symbolsError}
                onChange={(event) => {
                  setDirty(true)
                  setSymbolsInput(event.target.value)
                }}
              />
              <span className="text-muted-foreground text-xs">
                Comma-separated, uppercased and de-duplicated automatically
              </span>
              {symbolsError && <p role="alert">{symbolsError}</p>}
            </div>

            <div className="flex gap-2">
              <Button
                type="button"
                disabled={startDisabled}
                onClick={onStart}
                className="flex-1"
              >
                <Play className="mr-1 size-4" />
                {startMutation.isPending ? "Starting…" : "Start"}
              </Button>
              <Button
                type="button"
                variant="outline"
                disabled={stopDisabled}
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
          <CardTitle>{activeLabel} settings</CardTitle>
          <CardDescription>
            Fixed at start; the agent validates them before the strategy runs
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!catalog ? (
            <p role="status">
              {isCatalogError
                ? "Strategy settings are unavailable."
                : "Loading strategy settings…"}
            </p>
          ) : catalog.available === false ? (
            <p role="status">
              The agent has not published a strategy catalog yet, so Start is
              disabled. It will appear once the agent runs and publishes one.
            </p>
          ) : fields.length === 0 ? (
            <p role="status">
              The agent publishes no configurable settings for this strategy.
            </p>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {fields.map((field) => (
                <div key={field.key} className="flex flex-col gap-2">
                  <Label htmlFor={field.key}>{field.title}</Label>
                  {field.kind === "const" ? (
                    <Input
                      id={field.key}
                      value={field.defaultText}
                      readOnly
                      disabled
                    />
                  ) : field.kind === "enum" ? (
                    <>
                      <Input
                        id={field.key}
                        list={`${field.key}-options`}
                        value={values[field.key] ?? ""}
                        disabled={pending}
                        aria-invalid={!!fieldErrors[field.key]}
                        onChange={(event) =>
                          setValue(field.key, event.target.value)
                        }
                      />
                      <datalist id={`${field.key}-options`}>
                        {(field.options ?? []).map((option) => (
                          <option key={option} value={option} />
                        ))}
                      </datalist>
                    </>
                  ) : (
                    <Input
                      id={field.key}
                      value={values[field.key] ?? ""}
                      disabled={pending}
                      aria-invalid={!!fieldErrors[field.key]}
                      inputMode={field.kind === "text" ? undefined : "decimal"}
                      onChange={(event) =>
                        setValue(field.key, event.target.value)
                      }
                    />
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={pendingLiveStart !== null}
        onOpenChange={(open) => {
          if (!open) setPendingLiveStart(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Confirm live Start</DialogTitle>
            <DialogDescription>
              This sends a Start command on the LIVE network. Review before
              confirming.
            </DialogDescription>
          </DialogHeader>
          {pendingLiveStart && (
            <div className="space-y-2 text-sm">
              <p>Network: {pendingLiveStart.network.toUpperCase()}</p>
              <p>
                Strategy: {activeStrategy?.label ?? pendingLiveStart.strategy}
              </p>
              <p>Symbols: {pendingLiveStart.symbols.join(", ")}</p>
              <div className="space-y-1">
                <p className="font-medium">Key params</p>
                <ul className="grid max-h-64 grid-cols-2 gap-x-4 gap-y-1 overflow-y-auto text-xs">
                  {fields.map((field) => (
                    <li key={field.key}>
                      {field.title}:{" "}
                      {formatParamValue(pendingLiveStart.params[field.key])}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setPendingLiveStart(null)}>
              Cancel
            </Button>
            <Button
              onClick={() => {
                if (pendingLiveStart) runStart(pendingLiveStart)
                setPendingLiveStart(null)
              }}
            >
              Confirm live Start
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
