import { useQuery, useQueryClient } from "@tanstack/react-query"
import { createFileRoute } from "@tanstack/react-router"
import type { ColumnDef } from "@tanstack/react-table"
import { RefreshCw } from "lucide-react"
import { useMemo } from "react"

import { type SignalPositionPublic, TradingService } from "@/client"
import { DataTable } from "@/components/Common/DataTable"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import useAuth from "@/hooks/useAuth"

export const Route = createFileRoute("/_layout/")({
  component: Dashboard,
  head: () => ({
    meta: [{ title: "Dashboard - Mentis Trading Agent" }],
  }),
})

// Aligns refreshes to the strategy's own 15-minute ripple cadence (closed
// bars land on :00/:15/:30/:45; +1 minute gives the agent's own background
// refreshes — balance every 30s, prices every ~5min — time to have already
// landed) instead of drifting like a plain fixed interval would.
const REFRESH_MARKS_PAST_HOUR = [1, 16, 31, 46]

function msUntilNextRefreshMark(): number {
  const now = new Date()
  const next = new Date(now)
  next.setSeconds(0, 0)
  const upcoming = REFRESH_MARKS_PAST_HOUR.find((mark) => mark > now.getMinutes())
  if (upcoming === undefined) {
    next.setHours(next.getHours() + 1)
    next.setMinutes(REFRESH_MARKS_PAST_HOUR[0])
  } else {
    next.setMinutes(upcoming)
  }
  return Math.max(1000, next.getTime() - now.getTime())
}

function statusBadge(status: string) {
  if (status === "hit_take") return <Badge>Take</Badge>
  if (status === "hit_stop") return <Badge variant="destructive">Stop</Badge>
  return <Badge variant="secondary">Open</Badge>
}

function Dashboard() {
  const { user: currentUser } = useAuth()
  const queryClient = useQueryClient()

  const statusQuery = useQuery({
    queryKey: ["trading-status"],
    queryFn: async () => (await TradingService.getStatus()).data,
    refetchInterval: msUntilNextRefreshMark,
  })
  const positionsQuery = useQuery({
    queryKey: ["trading-positions"],
    queryFn: async () => (await TradingService.listPositions()).data,
    refetchInterval: msUntilNextRefreshMark,
  })

  const balanceRows = useMemo(
    () =>
      Object.entries(statusQuery.data?.actual?.balances ?? {}).sort(
        ([, a], [, b]) => b - a,
      ),
    [statusQuery.data],
  )

  const columns = useMemo<ColumnDef<SignalPositionPublic>[]>(
    () => [
      { accessorKey: "symbol", header: "Symbol" },
      { accessorKey: "network", header: "Network" },
      { accessorKey: "entry_ref", header: "Entry" },
      { accessorKey: "stop_ref", header: "Stop" },
      { accessorKey: "take_ref", header: "Take" },
      {
        id: "current",
        header: "Current",
        cell: ({ row }) =>
          row.original.closed_price ?? row.original.current_price ?? "—",
      },
      {
        accessorKey: "pnl_pct",
        header: "P&L %",
        cell: ({ row }) =>
          row.original.pnl_pct !== null
            ? `${Number(row.original.pnl_pct).toFixed(2)}%`
            : "—",
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: ({ row }) => statusBadge(row.original.status),
      },
      {
        accessorKey: "emitted_at",
        header: "Since",
        cell: ({ row }) => new Date(row.original.emitted_at).toLocaleString(),
      },
    ],
    [],
  )

  const refreshAll = () => {
    queryClient.invalidateQueries({ queryKey: ["trading-status"] })
    queryClient.invalidateQueries({ queryKey: ["trading-positions"] })
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-bold tracking-tight truncate max-w-sm">
            Hi, {currentUser?.full_name || currentUser?.email} 👋
          </h1>
          <p className="text-muted-foreground">
            Account balance and ENTRY-signal-derived positions, refreshed
            every 15 minutes and on demand.
          </p>
        </div>
        <Button
          variant="outline"
          onClick={refreshAll}
          disabled={statusQuery.isFetching || positionsQuery.isFetching}
        >
          <RefreshCw className="size-4" />
          Обновить сейчас
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Account balance</CardTitle>
          <CardDescription>
            {statusQuery.data?.actual?.network?.toUpperCase() ??
              "Unknown network"}{" "}
            — heartbeat snapshot, refreshed by the agent every 30s.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {balanceRows.length === 0 ? (
            <p className="text-muted-foreground">No non-zero balances.</p>
          ) : (
            <table className="w-full text-sm">
              <tbody>
                {balanceRows.map(([asset, amount]) => (
                  <tr key={asset} className="border-b last:border-0">
                    <td className="py-1 pr-4 font-medium">{asset}</td>
                    <td className="py-1 text-right font-mono">{amount}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Positions</CardTitle>
          <CardDescription>
            Derived from ENTRY signals — the strategy never places a real
            order (D-SIGNAL-ONLY), so this shows "what would have happened",
            not an actual exchange position. Closed once the last known price
            crossed stop or take.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <DataTable
            columns={columns}
            data={positionsQuery.data?.positions ?? []}
            sorting
          />
        </CardContent>
      </Card>
    </div>
  )
}
