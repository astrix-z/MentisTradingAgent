import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { TriangleAlert } from "lucide-react"
import { useCallback, useEffect, useMemo, useState } from "react"

import { type SymbolEntry, TradingService, UsersService } from "@/client"
import { CommandTrackingView } from "@/components/CommandTrackingView"
import { DataTable } from "@/components/Common/DataTable"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Switch } from "@/components/ui/switch"
import {
  latestTrackedCommand,
  type TrackedCommand,
  useCommandTracking,
} from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

export const Route = createFileRoute("/_layout/working-symbols")({
  component: WorkingSymbols,
  beforeLoad: async () => {
    const { data: user } = await UsersService.readUserMe()
    if (!user.is_superuser) throw redirect({ to: "/" })
  },
  head: () => ({ meta: [{ title: "Working Symbols - FastAPI Template" }] }),
})

const workingSymbolsQueryOptions = () => ({
  queryFn: async () => (await TradingService.listWorkingSymbols()).data,
  queryKey: ["working-symbols"],
  // The agent republishes the universe periodically, so volumes and listings
  // catch up on their own. Marks are only re-seeded while there is no draft
  // (see the seeding effect), so a refresh never discards local edits.
  refetchInterval: 60_000,
})

// Volumes arrive as Decimal strings and can be very large; group them so the
// column stays readable, and keep the exact value in the cell's title.
const volumeFormat = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 2,
})

// Prices arrive as Decimal strings too, but span nine orders of magnitude: a
// satoshi-priced memecoin and BTC both have to be legible, so the number of
// fraction digits is left to the value instead of being fixed.
const priceFormat = new Intl.NumberFormat("en-US", { maximumFractionDigits: 8 })

const volumeOf = (entry: SymbolEntry) =>
  Number(entry.quote_volume_24h ?? 0) || 0

const priceOf = (entry: SymbolEntry): number | null => {
  const price = entry.last_price ?? ""
  return price === "" ? null : Number(price)
}

/**
 * The threshold from the input as a number, or ``null`` when it is not one.
 *
 * Grouping separators are stripped because the table shows volumes grouped with
 * commas, so copying a value out of it is a normal thing to do.
 */
function parseVolumeThreshold(raw: string): number | null {
  const text = raw.replace(/[,\s_]/g, "")
  if (text === "") return null
  const value = Number(text)
  return Number.isFinite(value) ? value : null
}

function Marks({ entry }: { entry: SymbolEntry }) {
  const reason = entry.excluded_reason ?? null
  const label = entry.classification_label ?? null
  return (
    <div className="flex flex-wrap items-center gap-1">
      {entry.selected === true && reason !== null && (
        <Badge variant="destructive">marked, but unusable</Badge>
      )}
      {reason !== null ? (
        <Badge variant="outline">{reason}</Badge>
      ) : label !== null ? (
        <Badge variant="secondary">{label}</Badge>
      ) : null}
      {/* A warning, not a verdict: Binance reviews these assets periodically and
          may delist them, but they trade normally, so this never filters or
          blocks anything. */}
      {entry.monitoring === true && (
        <Badge
          variant="outline"
          className="gap-1 border-amber-500/60 text-amber-700 dark:text-amber-400"
          title="Binance has this asset under its Monitoring tag: it trades normally, but the exchange reviews it periodically and may delist it."
        >
          <TriangleAlert className="size-3" aria-hidden="true" />
          Monitoring
        </Badge>
      )}
      {reason === null && label === null && entry.monitoring !== true && (
        <span className="text-muted-foreground">—</span>
      )}
    </div>
  )
}

function WorkingSymbols() {
  const queryClient = useQueryClient()
  const { showErrorToast } = useCustomToast()
  const { commands, track } = useCommandTracking()
  const { data: document, isError } = useQuery(workingSymbolsQueryOptions())

  const [showExcluded, setShowExcluded] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [selection, setSelection] = useState<string[]>([])
  const [thresholdInput, setThresholdInput] = useState("")

  // Only the newest of each: a repeated request replaces its section rather than
  // stacking another one, and its result is what reports what happened.
  const latestSymbolsCommand = latestTrackedCommand(
    commands,
    (command) => command.envelope.type === "symbols",
  )
  const latestPricesCommand = latestTrackedCommand(
    commands,
    (command) => command.envelope.type === "prices",
  )

  // The agent is the source of truth for what is marked. Seed the draft from
  // its document, but never over a draft the user is still editing.
  useEffect(() => {
    if (dirty || !document) return
    setSelection(
      (document.symbols ?? [])
        .filter((entry) => entry.selected === true)
        .map((entry) => entry.symbol),
    )
  }, [document, dirty])

  // A confirmed save is what makes the draft the saved state, so only then does
  // the document replace it.
  useEffect(() => {
    if (latestSymbolsCommand?.phase === "succeeded") {
      setDirty(false)
      void queryClient.invalidateQueries({ queryKey: ["working-symbols"] })
    }
  }, [latestSymbolsCommand?.phase, queryClient])

  // A confirmed price refresh only re-reads the document, so it must not touch
  // the draft: marks the user has not saved yet stay theirs.
  useEffect(() => {
    if (latestPricesCommand?.phase === "succeeded") {
      void queryClient.invalidateQueries({ queryKey: ["working-symbols"] })
    }
  }, [latestPricesCommand?.phase, queryClient])

  const selected = useMemo(() => new Set(selection), [selection])

  const saveMutation = useMutation({
    mutationFn: (symbols: string[]) =>
      TradingService.setWorkingSymbols({ body: { symbols } }),
    onSuccess: ({ data }) => track(data),
    onError: handleError.bind(showErrorToast),
  })
  const pricesMutation = useMutation({
    mutationFn: () => TradingService.refreshPrices(),
    onSuccess: ({ data }) => track(data),
    onError: handleError.bind(showErrorToast),
  })

  const toggle = useCallback((symbol: string) => {
    setDirty(true)
    setSelection((current) =>
      current.includes(symbol)
        ? current.filter((item) => item !== symbol)
        : [...current, symbol],
    )
  }, [])

  const entries = document?.symbols ?? []
  const visible = useMemo(() => {
    if (showExcluded) return entries
    // A marked symbol stays visible even after it stopped qualifying, or the
    // user could not unmark it.
    return entries.filter(
      (entry) =>
        (entry.excluded_reason ?? null) === null || selected.has(entry.symbol),
    )
  }, [entries, showExcluded, selected])

  // Which symbols may be marked at all. The bulk actions below skip everything
  // else, so a halted or tokenized-equity symbol is never selected by accident,
  // however large its volume.
  const usableEntries = entries.filter(
    (entry) => (entry.excluded_reason ?? null) === null,
  )
  const threshold = parseVolumeThreshold(thresholdInput)

  const markMany = useCallback((symbols: string[]) => {
    setDirty(true)
    setSelection((current) => Array.from(new Set([...current, ...symbols])))
  }, [])

  // Bulk actions edit the draft once and then forget how it was produced: no
  // selection rule is stored or reapplied on reload or when the strategy starts.
  const selectAll = () => markMany(usableEntries.map((entry) => entry.symbol))
  const selectMonitoring = () =>
    markMany(
      usableEntries
        .filter((entry) => entry.monitoring === true)
        .map((entry) => entry.symbol),
    )
  const selectAboveThreshold = () => {
    if (threshold === null) return
    markMany(
      usableEntries
        .filter((entry) => volumeOf(entry) > threshold)
        .map((entry) => entry.symbol),
    )
  }
  const invertSelection = () => {
    const usableSymbols = new Set(usableEntries.map((entry) => entry.symbol))
    setDirty(true)
    setSelection((current) => {
      const currentSet = new Set(current)
      return [
        // Preserve stale unusable marks so an action on hidden usable rows does
        // not silently discard them. They can still be unmarked explicitly.
        ...current.filter((symbol) => !usableSymbols.has(symbol)),
        ...usableEntries
          .filter((entry) => !currentSet.has(entry.symbol))
          .map((entry) => entry.symbol),
      ]
    })
  }

  const columns = useMemo<ColumnDef<SymbolEntry>[]>(
    () => [
      {
        id: "selected",
        accessorFn: (entry) => selected.has(entry.symbol),
        header: "In strategy",
        cell: ({ row }) => {
          const entry = row.original
          const isChecked = selected.has(entry.symbol)
          return (
            <Checkbox
              checked={isChecked}
              // An unusable symbol cannot be marked, but a marked one can always
              // be unmarked, so a stale mark is never stuck.
              disabled={(entry.excluded_reason ?? null) !== null && !isChecked}
              aria-label={`Use ${entry.symbol} in the strategy`}
              onCheckedChange={() => toggle(entry.symbol)}
            />
          )
        },
      },
      {
        accessorKey: "symbol",
        header: "Symbol",
        cell: ({ row }) => (
          <span className="font-mono font-medium">{row.original.symbol}</span>
        ),
      },
      {
        accessorKey: "last_price",
        header: "Price",
        // Decimal strings again: "9" must not beat "10". A symbol the exchange
        // priced nothing for sorts lowest rather than poisoning the comparison
        // with a NaN.
        sortingFn: (left, right) =>
          (priceOf(left.original) ?? -1) - (priceOf(right.original) ?? -1),
        cell: ({ row }) => {
          const price = priceOf(row.original)
          return price === null ? (
            <span className="text-muted-foreground">—</span>
          ) : (
            <span
              className="font-mono"
              title={row.original.last_price ?? undefined}
            >
              {priceFormat.format(price)}
            </span>
          )
        },
      },
      {
        accessorKey: "quote_volume_24h",
        header: "24h volume (USDT)",
        // Decimal strings must not be compared as text, or "9" would beat "10".
        sortingFn: (left, right) =>
          volumeOf(left.original) - volumeOf(right.original),
        cell: ({ row }) => (
          <span title={row.original.quote_volume_24h ?? "0"}>
            {volumeFormat.format(volumeOf(row.original))}
          </span>
        ),
      },
      {
        id: "marks",
        accessorFn: (entry) =>
          [
            entry.excluded_reason ?? entry.classification_label,
            entry.monitoring === true ? "monitoring" : null,
          ]
            .filter(Boolean)
            .join(" · "),
        header: "Marks",
        cell: ({ row }) => <Marks entry={row.original} />,
      },
    ],
    [selected, toggle],
  )

  const available = document?.available !== false
  const usable = document?.usable ?? 0
  const total = document?.total ?? 0
  const unusableMarked = document?.selected_unusable ?? 0
  const monitored = entries.filter((entry) => entry.monitoring === true).length
  // Say how old the prices are instead of letting them look live: they are read
  // when the agent publishes or when someone asks, not continuously.
  const pricesReadAt = document?.prices_updated_at
  const pricesFreshness = pricesReadAt
    ? `Prices read at ${new Date(pricesReadAt).toLocaleTimeString()}`
    : "No prices read yet"
  // The monitoring list is read daily, so its age is a date, not a clock time.
  const monitoringReadAt = document?.monitoring_updated_at
  const monitoringFreshness = monitoringReadAt
    ? `Monitoring list read ${new Date(monitoringReadAt).toLocaleString()}`
    : "Monitoring list not read yet"

  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="text-2xl font-bold tracking-tight">Working Symbols</h1>
        <p className="text-muted-foreground">
          Choose which of the exchange's tradeable symbols the strategy may use.
          The agent checks every symbol against the exchange, stores the marks
          in its own database, and restores them when it restarts.
        </p>
      </div>

      {isError && (
        <p role="alert">
          Cannot load the symbol list from the agent. Start is disabled until it
          is available.
        </p>
      )}

      {document && !available && (
        <p role="status">
          The agent has not published a symbol list yet. It appears once the
          agent runs and publishes one.
        </p>
      )}

      {document && available && (
        <>
          <div className="flex flex-wrap items-center gap-4">
            <p role="status">
              Marked: {selection.length} · Usable: {usable} · Listed: {total}
              {monitored > 0 ? ` · Monitoring: ${monitored}` : ""}
            </p>
            {unusableMarked > 0 && (
              <p role="alert" className="text-destructive">
                {unusableMarked} saved symbol(s) can no longer be used; unmark
                them or they will be ignored.
              </p>
            )}
            <div className="ml-auto flex flex-wrap items-center gap-3">
              <span className="text-muted-foreground text-sm">
                {monitoringFreshness}
              </span>
              <span className="text-muted-foreground text-sm">
                {pricesFreshness}
              </span>
              {/* Prices are read on demand rather than polled: the agent has to
                  ask the exchange, and the user is the one who knows when a
                  fresh number matters. */}
              <Button
                variant="outline"
                disabled={pricesMutation.isPending}
                onClick={() => pricesMutation.mutate()}
              >
                {pricesMutation.isPending ? "Requesting…" : "Request prices"}
              </Button>
            </div>
          </div>

          <div className="flex flex-col gap-3 rounded-md border p-3">
            <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
              <Button variant="outline" onClick={selectAll}>
                Select all
              </Button>
              <Button variant="outline" onClick={selectMonitoring}>
                Select Monitoring
              </Button>
              <Button variant="outline" onClick={invertSelection}>
                Invert selection
              </Button>
              <div className="flex items-end gap-2">
                <div className="flex flex-col gap-1">
                  <Label htmlFor="volume-threshold">
                    Volume above (24h, USDT)
                  </Label>
                  <Input
                    id="volume-threshold"
                    inputMode="decimal"
                    placeholder="e.g. 5000000"
                    className="h-9 w-44"
                    value={thresholdInput}
                    onChange={(event) => setThresholdInput(event.target.value)}
                  />
                </div>
                <Button
                  variant="outline"
                  disabled={threshold === null}
                  onClick={selectAboveThreshold}
                >
                  Select above
                </Button>
              </div>
            </div>
            <p className="text-muted-foreground text-sm">
              Bulk actions edit the draft at the moment you press them. No rule
              is saved or reapplied on reload or when the strategy starts.
              Unusable symbols are never selected or changed by these actions.
            </p>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-2">
              <Switch
                id="show-excluded"
                checked={showExcluded}
                onCheckedChange={setShowExcluded}
              />
              <Label htmlFor="show-excluded">Show unusable symbols</Label>
            </div>
            <div className="flex items-center gap-2">
              {dirty && (
                <span className="text-muted-foreground text-sm">
                  Unsaved changes
                </span>
              )}
              <Button
                variant="outline"
                disabled={!dirty || saveMutation.isPending}
                onClick={() => setDirty(false)}
              >
                Reset
              </Button>
              <Button
                disabled={!dirty || saveMutation.isPending}
                onClick={() => saveMutation.mutate([...selection].sort())}
              >
                {saveMutation.isPending ? "Saving…" : "Save"}
              </Button>
            </div>
          </div>

          <DataTable columns={columns} data={visible} sorting />

          <CommandTrackingView
            commands={[latestSymbolsCommand, latestPricesCommand].filter(
              (command): command is TrackedCommand => command !== undefined,
            )}
          />

          <p className="text-muted-foreground text-sm">
            Symbols the exchange does not allow (tokenized equities,
            stablecoins, wrapped or staked tokens, gold, halted markets) cannot
            be marked. Prices are read when the agent publishes and whenever you
            ask for them, so they are as old as the time above says. The
            Monitoring tag is read once a day from Binance's public markets
            listing — an unofficial source, since Binance publishes no API for
            it — and it is a label: a monitored symbol still trades, and is
            still markable.{" "}
            <Link to="/strategy" className="underline">
              Back to Strategy
            </Link>
          </p>
        </>
      )}
    </div>
  )
}
