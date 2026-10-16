import { useMutation, useQueryClient } from "@tanstack/react-query"
import { Pause, Play } from "lucide-react"
import { useEffect, useMemo, useState } from "react"

import {
  type StrategyDescription,
  TradingService,
  type TradingStatus,
} from "@/client"
import { CommandTrackingView } from "@/components/CommandTrackingView"
import { StrategyFieldInput } from "@/components/strategy-form/StrategyFieldInput"
import {
  type FieldValue,
  type FieldValues,
  formatParamValue,
  parseSettingsSchema,
  sameParams,
  seedFormValues,
  type ValidationIssue,
  validateSettings,
} from "@/components/strategy-form/strategySchema"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import {
  latestTrackedCommand,
  type TrackedCommand,
  useCommandTracking,
} from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

/** The switch itself is not a limit: it has its own control. */
const SWITCH_FIELD = "enabled"

const EXECUTION_COMMANDS = new Set([
  "execution_configure",
  "execution_pause",
  "execution_resume",
])
const isExecutionCommand = (command: TrackedCommand) =>
  EXECUTION_COMMANDS.has(command.envelope.type)

/** What the agent's heartbeat mode means, in words. */
export function executionModeLabel(mode: string | undefined): string {
  if (!mode) return "unknown"
  if (mode === "off") return "off — signals only, no orders"
  if (mode === "enabled") return "on — placing orders"
  const reason = /^paused\((.*)\)$/.exec(mode)?.[1]
  if (reason === "paused_by_owner") return "paused by the owner"
  if (reason === "no_execution_lock")
    return "on, taking the lock and reconciling with the exchange (can take minutes)"
  if (reason) return `on, entries held: ${reason}`
  return mode
}

/**
 * Real trading: the owner's switch and its limits, apart from the network
 * choice and from Start (ClickUp 869fgqxah). Turning it on asks for a
 * confirmation that names the network the strategy runs on; turning it off
 * does not. Open positions are accompanied either way.
 */
export function ExecutionControl({
  status,
  section,
}: {
  status?: TradingStatus
  section?: StrategyDescription | null
}) {
  const { showErrorToast } = useCustomToast()
  const queryClient = useQueryClient()
  const { commands, track } = useCommandTracking()

  const desired = status?.execution?.settings ?? {}
  const desiredOn = desired[SWITCH_FIELD] === true
  const mode = status?.actual?.execution?.mode
  const paused = mode === "paused(paused_by_owner)"
  // The network real orders would go to: the running strategy's, else the
  // one the next Start is set to.
  const network = (
    status?.actual?.state === "running"
      ? status.actual.network
      : status?.desired.network
  )?.toUpperCase()

  const fields = useMemo(
    () =>
      parseSettingsSchema(section?.settings_schema).filter(
        (field) => field.key !== SWITCH_FIELD,
      ),
    [section],
  )
  const [values, setValues] = useState<FieldValues>({})
  const [dirty, setDirty] = useState(false)
  const [issues, setIssues] = useState<ValidationIssue[]>([])
  const [confirming, setConfirming] = useState<Record<string, unknown> | null>(
    null,
  )

  const desiredKey = JSON.stringify(desired)
  // Untouched limits follow the stored ones (another tab, another admin).
  useEffect(() => {
    if (dirty || fields.length === 0) return
    setValues(seedFormValues(fields, JSON.parse(desiredKey)))
  }, [dirty, fields, desiredKey])

  const configure = useMutation({
    mutationFn: (settings: Record<string, unknown>) =>
      TradingService.configureExecution({ body: { settings } }),
    onSuccess: ({ data }) => {
      track(data)
      setDirty(false)
      void queryClient.invalidateQueries({ queryKey: ["trading-status"] })
    },
    onError: handleError.bind(showErrorToast),
  })
  const pauseOrResume = useMutation({
    mutationFn: (type: "execution_pause" | "execution_resume") =>
      TradingService.issueCommand({ body: { type, payload: {} } }),
    onSuccess: ({ data }) => track(data),
    onError: handleError.bind(showErrorToast),
  })
  const busy = configure.isPending || pauseOrResume.isPending
  const latest = latestTrackedCommand(commands, isExecutionCommand)

  const checkLimits = (draft: FieldValues = values) =>
    validateSettings({
      fields,
      values: draft,
      lookups: {},
      constraints: section?.constraints ?? [],
      optionLists: {},
    })
  /** The limits in the form, checked; `null` (and the issues shown) if not. */
  const checkedLimits = (): Record<string, unknown> | null => {
    const result = checkLimits()
    if (result.status === "valid") {
      setIssues([])
      return result.params
    }
    setIssues(
      result.status === "invalid"
        ? result.issues
        : [{ type: "unsupported", field: null, message: result.reason }],
    )
    return null
  }

  const onSwitch = (on: boolean) => {
    if (!on) {
      // Never blocked by the limits, and never a silent change of them: off
      // with the stored limits when they still pass, else the form's, else
      // the agent's defaults (review 869fgqxah, P3-1, P3-c).
      const stored = checkLimits(seedFormValues(fields, desired))
      const form = checkLimits(values)
      const limits =
        stored.status === "valid"
          ? stored.params
          : form.status === "valid"
            ? form.params
            : {}
      configure.mutate({ ...limits, [SWITCH_FIELD]: false })
      return
    }
    const limits = checkedLimits()
    if (limits) setConfirming(limits)
  }
  const onSaveLimits = () => {
    const limits = checkedLimits()
    if (!limits) return
    // Stored ON but the agent is off (it refused, or has not applied it):
    // saving would switch real trading on again — confirm that like a switch.
    if (desiredOn && mode === "off") {
      setConfirming(limits)
      return
    }
    configure.mutate({ ...limits, [SWITCH_FIELD]: desiredOn })
  }
  const onConfirm = () => {
    if (!confirming) return
    configure.mutate({ ...confirming, [SWITCH_FIELD]: true })
    setConfirming(null)
  }

  const changed = dirty && !sameParams(fields, values, desired)
  const awaiting =
    status?.online === true &&
    mode !== undefined &&
    desiredOn !== (mode !== "off")

  if (!section)
    return (
      <p role="status">
        The agent has not published its real-trading settings yet, so real
        trading cannot be switched on.
      </p>
    )

  return (
    <section className="flex flex-col gap-4 text-sm" aria-label="Real trading">
      <div className="flex items-center gap-3">
        <Switch
          id="real-trading"
          checked={desiredOn}
          disabled={busy || !status}
          onCheckedChange={onSwitch}
          aria-describedby="real-trading-mode"
        />
        <Label htmlFor="real-trading" className="font-semibold">
          Real trading {desiredOn ? "ON" : "OFF"}
        </Label>
      </div>
      <p id="real-trading-mode">
        Agent: {executionModeLabel(mode)}
        {network ? ` · network ${network}` : ""}
        {status?.actual?.execution?.open_positions
          ? ` · open positions: ${status.actual.execution.open_positions}`
          : ""}
      </p>
      {awaiting && (
        <p role="status">
          The agent has not applied the switch yet; if it refused, the reason is
          shown below.
        </p>
      )}
      <p className="text-muted-foreground">
        Independent of the network choice and of Start / Stop. Off, the strategy
        only records signals; open positions are always accompanied and
        protected.
      </p>

      {desiredOn && (
        <div className="flex gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={busy || paused}
            onClick={() => pauseOrResume.mutate("execution_pause")}
          >
            <Pause className="mr-1 size-4" />
            Pause entries
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={busy || !paused}
            onClick={() => pauseOrResume.mutate("execution_resume")}
          >
            <Play className="mr-1 size-4" />
            Resume entries
          </Button>
        </div>
      )}

      <details open={dirty || issues.length > 0}>
        <summary className="cursor-pointer select-none font-semibold">
          Limits
        </summary>
        <div className="mt-3 grid gap-4 sm:grid-cols-2">
          {fields.map((field) => (
            <StrategyFieldInput
              key={field.key}
              field={field}
              value={values[field.key] ?? field.defaultValue}
              issue={issues.find((issue) => issue.field === field.key)}
              disabled={busy}
              onChange={(value: FieldValue) => {
                setDirty(true)
                setValues((current) => ({ ...current, [field.key]: value }))
              }}
            />
          ))}
        </div>
        {issues
          .filter((issue) => issue.field === null)
          .map((issue) => (
            <p key={issue.message} role="alert" className="text-destructive">
              {issue.message}
            </p>
          ))}
        <div className="mt-3 flex gap-2">
          <Button
            type="button"
            disabled={busy || !changed}
            onClick={onSaveLimits}
          >
            Save limits
          </Button>
          {dirty && (
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={() => {
                setIssues([])
                setDirty(false)
              }}
            >
              Discard
            </Button>
          )}
        </div>
      </details>

      {latest && <CommandTrackingView commands={[latest]} />}

      <Dialog
        open={confirming !== null}
        onOpenChange={(open) => {
          if (!open) setConfirming(null)
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Turn real trading on</DialogTitle>
            <DialogDescription>
              The agent will place real orders for every admitted ENTRY on{" "}
              <strong>{network ?? "the network of the next Start"}</strong>
              {network === "LIVE" ? " — with real money" : ""}. Review the
              limits before confirming.
            </DialogDescription>
          </DialogHeader>
          {confirming && (
            <ul className="grid max-h-64 grid-cols-2 gap-x-4 gap-y-1 overflow-y-auto text-xs">
              {fields.map((field) => (
                <li key={field.key}>
                  {field.title}: {formatParamValue(confirming[field.key])}
                </li>
              ))}
            </ul>
          )}
          <DialogFooter className="sticky bottom-0 -mx-6 -mb-6 border-t bg-background px-6 py-4">
            <Button variant="outline" onClick={() => setConfirming(null)}>
              Cancel
            </Button>
            <Button onClick={onConfirm}>
              Turn on{network ? ` on ${network}` : ""}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}
