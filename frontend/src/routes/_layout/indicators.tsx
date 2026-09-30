import { useMutation, useQuery } from "@tanstack/react-query"
import { createFileRoute, redirect } from "@tanstack/react-router"

import { TradingService, UsersService } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import {
  latestTrackedCommand,
  type TrackedCommand,
  useCommandTracking,
} from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/indicators")({
  component: IndicatorsMonitor,
  beforeLoad: async () => {
    const { data: user } = await UsersService.readUserMe()
    if (!user.is_superuser) throw redirect({ to: "/" })
  },
  head: () => ({
    meta: [{ title: "Strategy Indicators Monitor - FastAPI Template" }],
  }),
})

// Shape of CommandResult.data for a succeeded `indicators` command
// (backend: IndicatorSnapshot in app/api/routes/trading.py). The strategy
// declares its own groups, so nothing here is specific to one strategy.
type IndicatorValue = { current: string | null; previous: string | null }
type GroupValues = {
  bar_open_time: string | null
  values: Record<string, IndicatorValue>
}
type IndicatorSnapshot = {
  strategy: string
  network: string
  computed_at: string
  groups: {
    key: string
    label: string
    interval: string
    indicators: { key: string; label: string }[]
  }[]
  rows: { symbol: string; groups: Record<string, GroupValues> }[]
}

function asSnapshot(data: unknown): IndicatorSnapshot | null {
  if (typeof data !== "object" || data === null) return null
  const candidate = data as Partial<IndicatorSnapshot>
  return Array.isArray(candidate.groups) && Array.isArray(candidate.rows)
    ? (candidate as IndicatorSnapshot)
    : null
}

/** ~6 significant digits; exponent for very large (FI) or tiny (PEPE) values. */
export function formatIndicator(raw: string | null | undefined): string {
  if (raw == null) return "—"
  const value = Number(raw)
  if (!Number.isFinite(value)) return raw
  if (value === 0) return "0"
  const magnitude = Math.abs(value)
  if (magnitude >= 1e6 || magnitude < 1e-4)
    return value.toExponential(5).replace(/\.?0+e/, "e")
  return String(Number(value.toPrecision(6)))
}

function formatBarTime(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime())
    ? iso
    : `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`
}

/** The bar time shared by every symbol in a group, or null if they differ. */
function commonBarTime(
  snapshot: IndicatorSnapshot,
  groupKey: string,
): string | null {
  const times = new Set(
    snapshot.rows
      .map((row) => row.groups[groupKey]?.bar_open_time)
      .filter((time): time is string => Boolean(time)),
  )
  return times.size === 1 ? [...times][0] : null
}

function IndicatorsMonitor() {
  const { data: status } = useQuery({
    queryFn: async () => (await TradingService.getStatus()).data,
    queryKey: ["trading-status"],
    refetchInterval: 5000,
  })
  const { commands, track } = useCommandTracking()
  const { showErrorToast } = useCustomToast()
  const isIndicators = (command: TrackedCommand) =>
    command.envelope.type === "indicators"
  const latest = latestTrackedCommand(commands, isIndicators)
  // Keep showing the last good table while a newer refresh is in flight.
  const shown = latestTrackedCommand(
    commands,
    (command) => isIndicators(command) && command.phase === "succeeded",
  )
  const snapshot = asSnapshot(shown?.result?.data)
  const refresh = useMutation({
    mutationFn: () => TradingService.fetchIndicators(),
    onSuccess: ({ data }) => track(data),
    onError: handleError.bind(showErrorToast),
  })
  const inFlight =
    refresh.isPending ||
    latest?.phase === "accepted" ||
    latest?.phase === "pending"
  const actual = status?.actual
  // The table shows what was read, from where it was read: a snapshot of
  // another network or strategy than the one running now is marked, never
  // relabelled with the current status.
  const mismatch =
    snapshot !== null &&
    actual != null &&
    ((actual.network != null && actual.network !== snapshot.network) ||
      (actual.strategy != null && actual.strategy !== snapshot.strategy))

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">
          Strategy Indicators Monitor
        </h1>
        <p className="text-muted-foreground">
          Indicator values of the running strategy on the last closed bar of
          each indicator&apos;s timeframe, for the symbols selected for work.
          Values are read on request only.
        </p>
      </div>
      <div className="flex flex-wrap items-center gap-3 text-sm">
        <Button disabled={inFlight} onClick={() => refresh.mutate()}>
          {inFlight ? "Refreshing…" : "Refresh"}
        </Button>
        <span>
          Agent: {actual?.state ?? "unknown"}
          {actual?.strategy ? ` · ${actual.strategy}` : ""}
          {actual?.network ? ` · ${actual.network.toUpperCase()}` : ""}
        </span>
        {snapshot && (
          <span className="text-muted-foreground" data-testid="computed-at">
            Updated {formatBarTime(snapshot.computed_at)}
            <span data-testid="snapshot-source">
              {` · read from ${snapshot.strategy} · ${snapshot.network.toUpperCase()}`}
            </span>
          </span>
        )}
      </div>
      {mismatch && snapshot && actual && (
        <p
          role="alert"
          className="text-amber-600 dark:text-amber-400"
          data-testid="snapshot-mismatch"
        >
          These values were read from {snapshot.strategy} on{" "}
          {snapshot.network.toUpperCase()}; the agent now runs{" "}
          {actual.strategy ?? "no strategy"} on{" "}
          {actual.network?.toUpperCase() ?? "no network"}. Press Refresh for
          current values.
        </p>
      )}
      {latest?.phase === "failed" && (
        <p role="alert">
          {latest.result?.message ?? "The agent could not read indicators."}
        </p>
      )}
      {latest?.phase === "timeout" && (
        <p role="alert">
          No answer from the agent within 120 seconds; values not refreshed.
        </p>
      )}
      {latest?.pollError && <p>{latest.pollError}</p>}
      {!latest && (
        <p className="text-muted-foreground">
          Press Refresh to read the current values.
        </p>
      )}
      {snapshot && <IndicatorTable snapshot={snapshot} />}
    </div>
  )
}

function IndicatorTable({ snapshot }: { snapshot: IndicatorSnapshot }) {
  if (snapshot.rows.length === 0)
    return <p>No symbols are selected for work right now.</p>
  const shared = Object.fromEntries(
    snapshot.groups.map((group) => [
      group.key,
      commonBarTime(snapshot, group.key),
    ]),
  )
  return (
    <div className="overflow-x-auto rounded-md border">
      <Table className="text-xs">
        <TableHeader>
          <TableRow>
            <TableHead rowSpan={2} className="align-bottom">
              Symbol
            </TableHead>
            {snapshot.groups.map((group) => (
              <TableHead
                key={group.key}
                colSpan={group.indicators.length}
                className="border-l text-center"
              >
                <div className="font-semibold">
                  {group.label} {group.interval}
                </div>
                {shared[group.key] && (
                  <div className="font-normal text-muted-foreground">
                    bar {formatBarTime(shared[group.key] as string)}
                  </div>
                )}
              </TableHead>
            ))}
          </TableRow>
          <TableRow>
            {snapshot.groups.flatMap((group) =>
              group.indicators.map((indicator, index) => (
                <TableHead
                  key={`${group.key}.${indicator.key}`}
                  className={index === 0 ? "border-l text-right" : "text-right"}
                >
                  {indicator.label}
                </TableHead>
              )),
            )}
          </TableRow>
        </TableHeader>
        <TableBody>
          {snapshot.rows.map((row) => (
            <TableRow key={row.symbol}>
              <TableCell className="font-medium">{row.symbol}</TableCell>
              {snapshot.groups.flatMap((group) => {
                const cell = row.groups[group.key]
                // Only stamp a cell when this symbol's bar is not the shared one.
                const ownTime =
                  cell?.bar_open_time &&
                  cell.bar_open_time !== shared[group.key]
                    ? formatBarTime(cell.bar_open_time)
                    : null
                return group.indicators.map((indicator, index) => {
                  const value = cell?.values[indicator.key]
                  return (
                    <TableCell
                      key={`${group.key}.${indicator.key}`}
                      className={
                        index === 0
                          ? "border-l text-right tabular-nums"
                          : "text-right tabular-nums"
                      }
                      title={
                        value ? undefined : "Not enough history for this value"
                      }
                    >
                      <div>{formatIndicator(value?.current)}</div>
                      {value && (
                        <div className="text-muted-foreground">
                          prev {formatIndicator(value.previous)}
                        </div>
                      )}
                      {ownTime && index === 0 && (
                        <div className="text-muted-foreground">
                          bar {ownTime}
                        </div>
                      )}
                    </TableCell>
                  )
                })
              })}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}
