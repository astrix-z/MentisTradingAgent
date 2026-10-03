import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import { Play, Square } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"

import {
  PresetsService,
  type StartPayload,
  TradingService,
  UsersService,
} from "@/client"
import { CommandTrackingView } from "@/components/CommandTrackingView"
import { ReleaseEntry } from "@/components/ReleaseEntry"
import { OptionListsEditor } from "@/components/strategy-form/OptionListsEditor"
import {
  CURRENT_PARAMS,
  type PresetEntry,
  PresetPicker,
} from "@/components/strategy-form/PresetPicker"
import { StrategyFieldInput } from "@/components/strategy-form/StrategyFieldInput"
import {
  type FieldValue,
  type FieldValues,
  formatParamValue,
  migrateDraft,
  type OptionLists,
  parseSettingsSchema,
  requiredHistory,
  sameParams,
  seedFormValues,
  type ValidationIssue,
  type ValidationResult,
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
import { Label } from "@/components/ui/label"
import {
  latestTrackedCommand,
  type TrackedCommand,
  useCommandTracking,
} from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { cn } from "@/lib/utils"
import { extractErrorMessage, handleError } from "@/utils"

type Network = NonNullable<StartPayload["network"]>
type ValidatedStart = {
  network: Network
  strategy: string
  symbols: string[]
  params: Record<string, unknown>
}
/** A live Start waiting for confirmation, with the context it was checked in. */
type PendingLiveStart = { payload: ValidatedStart; context: string }
/** The latest check of the draft and the draft + context it was run on. */
type Checked = { key: string; result: ValidationResult }
type ValidationState = "pending" | "unavailable" | "valid" | "invalid"

/** Live checks run this long after the last keystroke (§4.5.3). */
const VALIDATION_DEBOUNCE_MS = 200

const tradingStatusQueryOptions = () => ({
  queryFn: async () => (await TradingService.getStatus()).data,
  queryKey: ["trading-status"],
  refetchInterval: 5000,
})

// Re-read now and then: a redeployed agent publishes a new schema, and an
// open draft is carried over to it instead of being checked against the old.
const strategyCatalogQueryOptions = () => ({
  queryFn: async () => (await TradingService.listStrategies()).data,
  queryKey: ["strategy-catalog"],
  refetchInterval: 60_000,
})

// Another administrator may edit the lists; the backend re-checks anyway.
const fieldOptionsQueryOptions = (strategy: string) => ({
  queryFn: async () =>
    (await PresetsService.listOptions({ path: { strategy } })).data,
  queryKey: ["field-options", strategy],
  refetchInterval: 30_000,
})

// Reading presets syncs the built-in ones under a row lock, so they are read
// on page load and after an edit, not polled.
const presetsQueryOptions = (strategy: string) => ({
  queryFn: async () =>
    (await PresetsService.listPresets({ path: { strategy } })).data,
  queryKey: ["strategy-presets", strategy],
})

// Which symbols a Start carries is decided on /working-symbols, so this page
// only reads the published document back to show what will be traded.
const workingSymbolsQueryOptions = () => ({
  queryFn: async () => (await TradingService.listWorkingSymbols()).data,
  queryKey: ["working-symbols"],
  refetchInterval: 60_000,
})

// This page owns start/stop only: a balance request belongs to /balance, and
// showing its result here would put account amounts on the settings page.
const isLifecycleCommand = (command: TrackedCommand) =>
  command.envelope.type === "start" || command.envelope.type === "stop"

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
  const queryClient = useQueryClient()
  const { commands, track } = useCommandTracking()
  const { data: status, isError } = useQuery(tradingStatusQueryOptions())
  const { data: catalog, isError: isCatalogError } = useQuery(
    strategyCatalogQueryOptions(),
  )
  const { data: workingSymbols, isError: isWorkingSymbolsError } = useQuery(
    workingSymbolsQueryOptions(),
  )

  const [network, setNetwork] = useState<Network>("testnet")
  const [values, setValues] = useState<FieldValues>({})
  const [checked, setChecked] = useState<Checked | null>(null)
  const [pendingLiveStart, setPendingLiveStart] =
    useState<PendingLiveStart | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [schemaChanged, setSchemaChanged] = useState(false)
  const [selectedPreset, setSelectedPreset] = useState(CURRENT_PARAMS)

  const [dirty, setDirty] = useState(false)
  const [strategy, setStrategy] = useState("")

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
  const legacyKeys = useMemo(
    () => activeStrategy?.legacy_keys ?? {},
    [activeStrategy],
  )
  const offeredNames = useMemo(
    () => new Set((strategies ?? []).map((item) => item.name)),
    [strategies],
  )
  const activeLabel = activeStrategy?.label ?? "strategy"
  const catalogReady = !!catalog && catalog.available !== false
  const strategyOffered = strategy !== "" && offeredNames.has(strategy)
  const strategyName = activeStrategy?.name ?? strategy

  const optionsQuery = useQuery({
    ...fieldOptionsQueryOptions(strategyName),
    enabled: strategyName !== "",
  })
  const presetsQuery = useQuery({
    ...presetsQueryOptions(strategyName),
    enabled: strategyName !== "",
  })
  const optionLists = useMemo(() => {
    const lists: OptionLists = {}
    for (const [field, entry] of Object.entries(optionsQuery.data ?? {}))
      lists[field] = entry.options
    return lists
  }, [optionsQuery.data])

  // What a check depends on besides the draft: the schema and the lists. A
  // live Start confirmed in another context is cancelled (§4.5.3).
  const contextReady =
    catalogReady && !!activeStrategy && optionsQuery.isSuccess
  const context = JSON.stringify([
    catalog?.schema_hash ?? null,
    activeStrategy?.name ?? null,
    optionLists,
  ])
  const draftKey = `${context}|${JSON.stringify(values)}`

  const validate = useCallback(
    (draft: FieldValues): ValidationResult => {
      if (!catalogReady || !activeStrategy)
        return {
          status: "unavailable",
          reason:
            "The strategy catalog is unavailable, so settings cannot be checked.",
        }
      if (optionsQuery.isError)
        return {
          status: "unavailable",
          reason:
            "The option lists could not be loaded, so settings cannot be checked.",
        }
      if (!optionsQuery.isSuccess)
        return { status: "unavailable", reason: "Loading the option lists…" }
      return validateSettings({
        fields,
        values: draft,
        lookups: catalog?.lookups ?? {},
        constraints: activeStrategy.constraints ?? [],
        optionLists,
      })
    },
    [
      catalogReady,
      activeStrategy,
      optionsQuery.isError,
      optionsQuery.isSuccess,
      fields,
      catalog?.lookups,
      optionLists,
    ],
  )

  // Live check: after every edit (debounced), and whenever the desired
  // params, a chosen preset, the catalog or the lists change the draft or
  // its context — edited or not.
  useEffect(() => {
    const timer = setTimeout(
      () => setChecked({ key: draftKey, result: validate(values) }),
      VALIDATION_DEBOUNCE_MS,
    )
    return () => clearTimeout(timer)
  }, [draftKey, validate, values])

  const validationState: ValidationState = !contextReady
    ? "unavailable"
    : checked?.key !== draftKey
      ? "pending"
      : checked.result.status

  const presets = presetsQuery.data?.items
  const presetEntries: PresetEntry[] = useMemo(
    () =>
      (presets ?? []).map((preset) => ({
        ...preset,
        launchable:
          preset.valid !== false &&
          (!contextReady ||
            validate(seedFormValues(fields, preset.params, legacyKeys))
              .status === "valid"),
      })),
    [presets, contextReady, validate, fields, legacyKeys],
  )
  const defaultPreset = presetEntries.find(
    (preset) => preset.is_builtin && preset.name === "Default",
  )
  const presetsSettled = strategyName === "" || !presetsQuery.isPending

  // Hydration while the draft is untouched: the desired params, with the
  // preset they match selected; without a snapshot, `Default` (§4.5.1).
  useEffect(() => {
    if (dirty || !status || fields.length === 0 || !presetsSettled) return
    const snapshot = status.desired
    const hasSnapshot = Object.keys(snapshot.params ?? {}).length > 0
    if (!hasSnapshot && defaultPreset) {
      setValues(seedFormValues(fields, defaultPreset.params, legacyKeys))
      setSelectedPreset(defaultPreset.id)
    } else {
      const seeded = seedFormValues(fields, snapshot.params, legacyKeys)
      setValues(seeded)
      const match = presetEntries.find((preset) =>
        sameParams(fields, seeded, preset.params, legacyKeys),
      )
      setSelectedPreset(match?.id ?? CURRENT_PARAMS)
    }
    setNetwork(snapshot.network ?? "testnet")
    setStrategy(snapshot.strategy ?? "")
  }, [
    status,
    fields,
    dirty,
    legacyKeys,
    presetsSettled,
    presetEntries,
    defaultPreset,
  ])

  // A new schema under a dirty draft: keep the draft, move renamed keys,
  // drop unknown ones, and say so (§4.6). The check reruns by itself.
  const seenSchema = useRef<string | null | undefined>(undefined)
  const schemaHash = catalogReady ? (catalog?.schema_hash ?? null) : undefined
  useEffect(() => {
    if (schemaHash === undefined || fields.length === 0) return
    const previous = seenSchema.current
    seenSchema.current = schemaHash
    if (previous === undefined || previous === schemaHash || !dirty) return
    setValues((draft) => migrateDraft(fields, draft, legacyKeys))
    setSchemaChanged(true)
  }, [schemaHash, fields, legacyKeys, dirty])

  useEffect(() => {
    if (pendingLiveStart && pendingLiveStart.context !== context) {
      setPendingLiveStart(null)
      setNotice(
        "The agent's schema or the option lists changed while the live Start was awaiting confirmation; it was cancelled. Review the settings and start again.",
      )
    }
  }, [pendingLiveStart, context])

  // The symbols a Start carries are the ones marked on the Working Symbols
  // page: the agent owns that list, validates it against the exchange and
  // stores it. A marked symbol that stopped qualifying is named, not silently
  // traded.
  const markedSymbols = (workingSymbols?.symbols ?? []).filter(
    (entry) => entry.selected === true,
  )
  const startSymbols = markedSymbols
    .filter((entry) => (entry.excluded_reason ?? null) === null)
    .map((entry) => entry.symbol)
  const unusableMarked = markedSymbols.length - startSymbols.length

  const symbolsProblem = isWorkingSymbolsError
    ? "Cannot load the working symbols from the agent."
    : workingSymbols?.available === false
      ? "The agent has not published a symbol list yet."
      : startSymbols.length === 0
        ? "No symbols are marked yet. Choose them on the Working Symbols page."
        : null

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
  const [presetBusy, setPresetBusy] = useState(false)

  const pending = startMutation.isPending || stopMutation.isPending

  // The latest tracked start/stop command drives the "awaiting confirmation"
  // and "timeout" control states; a 202 Accepted is never treated as success.
  const latestLifecycleCommand = useMemo(
    () => latestTrackedCommand(commands, isLifecycleCommand),
    [commands],
  )

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
  // published catalog, a strategy the agent actually offers and a draft that
  // is not known to be invalid; a pending check is rerun on the click.
  const startDisabled =
    pending ||
    !status ||
    !catalogReady ||
    !activeStrategy ||
    (strategy !== "" && !strategyOffered) ||
    !!symbolsProblem ||
    validationState === "unavailable" ||
    validationState === "invalid" ||
    controlState === "loading" ||
    (!dirty && controlState === "running-confirmed")
  const stopDisabled =
    pending ||
    controlState === "loading" ||
    controlState === "stopped-confirmed"

  const setValue = (key: string, value: FieldValue) => {
    setDirty(true)
    setValues((current) => ({ ...current, [key]: value }))
  }

  /** Check the exact draft now, show the outcome, return the params if valid. */
  const checkNow = (): Record<string, unknown> | null => {
    const result = validate(values)
    setChecked({ key: draftKey, result })
    return result.status === "valid" ? result.params : null
  }

  const runStart = (payload: ValidatedStart) => startMutation.mutate(payload)

  const onStart = () => {
    setNotice(null)
    if (!activeStrategy) return
    if (startSymbols.length === 0) return
    const params = checkNow()
    if (!params) return
    const payload: ValidatedStart = {
      network,
      strategy: strategy || activeStrategy.name,
      symbols: startSymbols,
      params,
    }
    if (network === "live") {
      setPendingLiveStart({ payload, context })
      return
    }
    runStart(payload)
  }

  const onConfirmLive = () => {
    if (!pendingLiveStart) return
    const params = checkNow()
    const unchanged =
      pendingLiveStart.context === context &&
      params !== null &&
      JSON.stringify(params) === JSON.stringify(pendingLiveStart.payload.params)
    setPendingLiveStart(null)
    if (!unchanged) {
      setNotice(
        "The settings no longer pass the check they were confirmed with; the live Start was cancelled.",
      )
      return
    }
    runStart(pendingLiveStart.payload)
  }

  const selectPreset = (id: string) => {
    setNotice(null)
    if (id === CURRENT_PARAMS) {
      // Back to the desired params: hydration seeds them again.
      setSelectedPreset(CURRENT_PARAMS)
      setDirty(false)
      return
    }
    const preset = presetEntries.find((entry) => entry.id === id)
    if (!preset) return
    // A preset replaces the whole form; nothing of the previous draft stays.
    setValues(seedFormValues(fields, preset.params, legacyKeys))
    setSelectedPreset(id)
    setDirty(true)
  }

  const presetRequest = async (
    request: (params: Record<string, unknown> | null) => Promise<void>,
    needsParams: boolean,
  ): Promise<string | null> => {
    const params = needsParams ? checkNow() : null
    if (needsParams && !params)
      return "The settings do not pass the check; fix them first."
    setPresetBusy(true)
    try {
      await request(params)
      await queryClient.invalidateQueries({
        queryKey: ["strategy-presets", strategyName],
      })
      return null
    } catch (error) {
      return extractErrorMessage(error as Error)
    } finally {
      setPresetBusy(false)
    }
  }

  const onSaveAs = (name: string) =>
    presetRequest(async (params) => {
      const { data } = await PresetsService.createPreset({
        path: { strategy: strategyName },
        body: { name, params: params ?? {} },
      })
      setSelectedPreset(data.id)
      setDirty(true)
    }, true)
  const onUpdate = () =>
    presetRequest(async (params) => {
      const preset = presetEntries.find((entry) => entry.id === selectedPreset)
      if (!preset) return
      await PresetsService.updatePreset({
        path: { strategy: strategyName, preset_id: preset.id },
        body: { name: preset.name, params: params ?? {} },
      })
    }, true)
  const onDelete = () =>
    presetRequest(async () => {
      await PresetsService.deletePreset({
        path: { strategy: strategyName, preset_id: selectedPreset },
      })
      setSelectedPreset(CURRENT_PARAMS)
    }, false)

  const selected = presetEntries.find((preset) => preset.id === selectedPreset)
  const modified =
    selected !== undefined &&
    !sameParams(fields, values, selected.params, legacyKeys)

  const shownResult = checked?.result
  const issues: ValidationIssue[] =
    shownResult?.status === "invalid" ? shownResult.issues : []
  const fieldIssue = (key: string) =>
    issues.find((issue) => issue.field === key)
  // Without a catalog the settings card says so itself.
  const unavailableResult = contextReady ? shownResult : validate(values)
  const unavailableReason =
    catalogReady &&
    validationState === "unavailable" &&
    unavailableResult?.status === "unavailable"
      ? unavailableResult.reason
      : null
  const history = useMemo(
    () =>
      new Map(
        requiredHistory(
          fields,
          values,
          catalog?.lookups ?? {},
          activeStrategy?.constraints ?? [],
        ).map((hint) => [hint.field, hint]),
      ),
    [fields, values, catalog?.lookups, activeStrategy],
  )
  const builtinSync = presetsQuery.data?.builtin_sync
  const presetsProblem = presetsQuery.isError
    ? "Presets could not be loaded."
    : null
  const canSave =
    catalogReady &&
    validationState !== "unavailable" &&
    validationState !== "invalid"

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Strategy</h1>
        <p className="text-muted-foreground">
          Configure the {activeLabel} strategy and start or stop the agent
        </p>
      </div>

      <CommandTrackingView
        commands={latestLifecycleCommand ? [latestLifecycleCommand] : []}
      />
      <p role="status">{CONTROL_STATE_COPY[controlState]}</p>
      <p>
        {dirty
          ? "Unsaved local draft — background updates will not overwrite it"
          : "Saved desired configuration"}
      </p>
      {dirty && (
        <Button
          variant="outline"
          onClick={() => {
            setSchemaChanged(false)
            setDirty(false)
          }}
        >
          Reload desired configuration
        </Button>
      )}
      {strategy !== "" && !strategyOffered && (
        <p role="alert">
          Saved strategy {strategy} is not offered by the agent. Start disabled.
        </p>
      )}
      {schemaChanged && (
        <p
          role="alert"
          className="rounded-md border border-amber-500 p-3 text-sm"
        >
          The agent's settings schema changed. Your draft was kept: renamed
          settings moved to their new names and removed ones were dropped. Check
          it before starting.
        </p>
      )}
      {notice && (
        <p role="alert" className="rounded-md border p-3 text-sm">
          {notice}
        </p>
      )}
      {unavailableReason && (
        <p role="alert" className="rounded-md border p-3 text-sm">
          {unavailableReason} Start and saving presets are disabled.
        </p>
      )}
      {issues.length > 0 && (
        <div role="alert" className="space-y-1 rounded-md border p-3 text-sm">
          <p className="font-semibold">Fix the following before starting:</p>
          {issues.map((issue) => (
            <p
              key={`${issue.field}:${issue.message}`}
              data-issue-type={issue.type}
              data-issue-field={issue.field ?? ""}
            >
              {issue.message}
            </p>
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
              <Label>Symbols</Label>
              {symbolsProblem ? (
                <p role="alert">{symbolsProblem}</p>
              ) : (
                <p>
                  {startSymbols.length} symbol(s) from the working list:{" "}
                  <span className="font-mono text-xs">
                    {startSymbols.slice(0, 12).join(", ")}
                    {startSymbols.length > 12
                      ? ` +${startSymbols.length - 12} more`
                      : ""}
                  </span>
                </p>
              )}
              <Link to="/working-symbols" className="text-sm underline">
                Working Symbols
              </Link>
              {unusableMarked > 0 && (
                <p className="text-destructive text-sm">
                  {unusableMarked} marked symbol(s) are unusable and will not be
                  traded.
                </p>
              )}
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

      {status?.actual?.execution?.alerts?.unknown && (
        <Card>
          <CardHeader>
            <CardTitle>Unresolved entry</CardTitle>
            <CardDescription>
              An entry whose outcome the agent could not prove; its reserve and
              slot stay held
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ReleaseEntry status={status} />
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle>{activeLabel} settings</CardTitle>
          <CardDescription>
            Fixed at start; the agent validates them before the strategy runs
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-6">
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
            <>
              <PresetPicker
                presets={presetEntries}
                selected={selectedPreset}
                modified={modified}
                loadProblem={presetsProblem}
                canSave={canSave}
                busy={presetBusy || pending}
                onSelect={selectPreset}
                onSaveAs={onSaveAs}
                onUpdate={onUpdate}
                onDelete={onDelete}
              />
              {builtinSync?.status === "partial" && (
                <div
                  role="alert"
                  className="rounded-md border border-amber-500 p-3 text-sm"
                >
                  <p className="font-semibold">
                    Some built-in presets from the agent were not accepted:
                  </p>
                  {(builtinSync.errors ?? []).map((error) => (
                    <p key={JSON.stringify(error)}>
                      {typeof error.name === "string"
                        ? error.name
                        : `#${String(error.index)}`}
                      : {String(error.type)}
                    </p>
                  ))}
                </div>
              )}
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {fields.map((field) => (
                  <StrategyFieldInput
                    key={field.key}
                    field={field}
                    value={values[field.key] ?? field.defaultValue}
                    optionList={optionLists[field.key]}
                    issue={fieldIssue(field.key)}
                    history={history.get(field.key)}
                    disabled={pending}
                    onChange={(value) => setValue(field.key, value)}
                  />
                ))}
              </div>
            </>
          )}
        </CardContent>
      </Card>

      {strategyName !== "" && (
        <OptionListsEditor
          strategy={strategyName}
          fields={fields}
          lists={optionsQuery.data}
          loadFailed={optionsQuery.isError}
          presets={presets ?? []}
          onSaved={async () => {
            await Promise.all([
              queryClient.invalidateQueries({
                queryKey: ["field-options", strategyName],
              }),
              queryClient.invalidateQueries({
                queryKey: ["strategy-presets", strategyName],
              }),
            ])
          }}
        />
      )}

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
              <p>Network: {pendingLiveStart.payload.network.toUpperCase()}</p>
              <p>
                Strategy:{" "}
                {activeStrategy?.label ?? pendingLiveStart.payload.strategy}
              </p>
              <div className="space-y-1">
                <p className="font-medium">
                  Symbols ({pendingLiveStart.payload.symbols.length})
                </p>
                <p className="max-h-32 overflow-y-auto rounded border p-2 text-xs">
                  {pendingLiveStart.payload.symbols.join(", ")}
                </p>
              </div>
              <div className="space-y-1">
                <p className="font-medium">Key params</p>
                <ul className="grid max-h-64 grid-cols-2 gap-x-4 gap-y-1 overflow-y-auto text-xs">
                  {fields.map((field) => (
                    <li key={field.key}>
                      {field.title}:{" "}
                      {formatParamValue(
                        pendingLiveStart.payload.params[field.key],
                      )}
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
          <DialogFooter className="sticky bottom-0 -mx-6 -mb-6 border-t bg-background px-6 py-4">
            <Button variant="outline" onClick={() => setPendingLiveStart(null)}>
              Cancel
            </Button>
            <Button onClick={onConfirmLive}>Confirm live Start</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
