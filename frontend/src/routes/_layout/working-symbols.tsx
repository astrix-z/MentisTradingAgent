import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute, Link, redirect } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
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

const volumeOf = (entry: SymbolEntry) =>
  Number(entry.quote_volume_24h ?? 0) || 0

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
      ) : (
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

  // Only the newest save: a repeated save replaces its section rather than
  // stacking another one, and the result is what reports rejected symbols.
  const latestSymbolsCommand = latestTrackedCommand(
    commands,
    (command) => command.envelope.type === "symbols",
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

  const selected = useMemo(() => new Set(selection), [selection])

  const saveMutation = useMutation({
    mutationFn: (symbols: string[]) =>
      TradingService.setWorkingSymbols({ body: { symbols } }),
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

  // Both bulk actions edit the draft once and then forget how it was produced:
  // the threshold is never stored as a rule, so it cannot be reapplied later —
  // neither on reload nor when the strategy starts.
  const selectAll = () => markMany(usableEntries.map((entry) => entry.symbol))
  const selectAboveThreshold = () => {
    if (threshold === null) return
    markMany(
      usableEntries
        .filter((entry) => volumeOf(entry) > threshold)
        .map((entry) => entry.symbol),
    )
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
          entry.excluded_reason ?? entry.classification_label ?? "",
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
            </p>
            {unusableMarked > 0 && (
              <p role="alert" className="text-destructive">
                {unusableMarked} saved symbol(s) can no longer be used; unmark
                them or they will be ignored.
              </p>
            )}
          </div>

          <div className="flex flex-col gap-3 rounded-md border p-3">
            <div className="flex flex-wrap items-end gap-x-4 gap-y-3">
              <Button variant="outline" onClick={selectAll}>
                Select all
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
              Both act on the draft at the moment you press them. The threshold
              is not saved as a rule, so it is never reapplied — neither on
              reload nor when the strategy starts. Unusable symbols are never
              selected.
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
            commands={latestSymbolsCommand ? [latestSymbolsCommand] : []}
          />

          <p className="text-muted-foreground text-sm">
            Symbols the exchange does not allow (tokenized equities,
            stablecoins, wrapped or staked tokens, gold, halted markets) cannot
            be marked.{" "}
            <Link to="/strategy" className="underline">
              Back to Strategy
            </Link>
          </p>
        </>
      )}
    </div>
  )
}
