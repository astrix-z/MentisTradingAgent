import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import type { TrackedCommand } from "@/hooks/useCommandTracking"

// Mirrors BalanceAsset / BalanceSnapshot in backend/app/api/routes/trading.py:
// CommandResult.data is polymorphic, so the shape is not in the generated client.
type BalanceAsset = { asset: string; free: string; locked: string }

function isBalanceSnapshot(
  data: unknown,
): data is { fetched_at?: string; assets: BalanceAsset[] } {
  return (
    typeof data === "object" &&
    data !== null &&
    Array.isArray((data as { assets?: unknown }).assets)
  )
}

function BalanceResult({ data }: { data: unknown }) {
  if (!isBalanceSnapshot(data)) return <JsonResult data={data} />
  return (
    <div className="space-y-2">
      {data.fetched_at && <p>Fetched at: {data.fetched_at}</p>}
      {data.assets.length === 0 ? (
        <p>No non-zero balances.</p>
      ) : (
        <Table aria-label="Balance">
          <TableHeader>
            <TableRow>
              <TableHead>Asset</TableHead>
              <TableHead className="text-right">Free</TableHead>
              <TableHead className="text-right">Locked</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {data.assets.map(({ asset, free, locked }) => (
              <TableRow key={asset}>
                <TableCell className="font-medium">{asset}</TableCell>
                <TableCell className="text-right font-mono">{free}</TableCell>
                <TableCell className="text-right font-mono">{locked}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </div>
  )
}

const LIFECYCLE_LABELS: Record<string, string> = {
  running: "Running",
  network: "Network",
  strategy: "Strategy",
  symbols: "Symbols",
}

function lifecycleValue(key: string, value: unknown): string {
  // The full symbol list is hundreds of entries; the count is what matters
  // here, the list itself is on the Strategy status and Working Symbols pages.
  if (Array.isArray(value)) return String(value.length)
  if (typeof value === "boolean") return value ? "yes" : "no"
  if (key === "network" && typeof value === "string") return value.toUpperCase()
  return String(value ?? "—")
}

function LifecycleResult({ data }: { data: unknown }) {
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    return <JsonResult data={data} />
  }
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1">
      {Object.entries(data).map(([key, value]) => (
        <div key={key} className="contents">
          <dt className="text-muted-foreground">
            {LIFECYCLE_LABELS[key] ?? key}
          </dt>
          <dd>{lifecycleValue(key, value)}</dd>
        </div>
      ))}
    </dl>
  )
}

function JsonResult({ data }: { data: unknown }) {
  return <pre className="overflow-auto">{JSON.stringify(data, null, 2)}</pre>
}

function CommandResultData({ type, data }: { type: string; data: unknown }) {
  if (type === "balance") return <BalanceResult data={data} />
  if (type === "start" || type === "stop")
    return <LifecycleResult data={data} />
  return <JsonResult data={data} />
}

export function CommandTrackingView({
  commands,
}: {
  commands: TrackedCommand[]
}) {
  return (
    <section
      aria-label="Command tracking"
      className="space-y-4"
      aria-live="polite"
    >
      {commands.map(({ envelope, phase, result, pollError }) => (
        <div
          key={envelope.id}
          className="space-y-1 rounded-md border p-3 text-sm"
          data-testid="command"
        >
          <p className="font-medium">
            {envelope.type} — {phase}
          </p>
          <p>Command ID: {envelope.id}</p>
          {envelope.type === "balance" && (
            <p>
              Requested balance network:{" "}
              {String(envelope.payload?.network ?? "unknown").toUpperCase()}
            </p>
          )}
          {phase === "accepted" && (
            <p>Command accepted; execution not yet confirmed.</p>
          )}
          {phase === "pending" && <p>Awaiting agent confirmation.</p>}
          {phase === "timeout" && (
            <p>
              No confirmation within 120 seconds. Execution outcome unknown; do
              not assume success or retry blindly.
            </p>
          )}
          {pollError && <p>{pollError}</p>}
          {result?.message && <p>{result.message}</p>}
          {result?.ok && result.data != null && (
            <div className="pt-2">
              <CommandResultData type={envelope.type} data={result.data} />
            </div>
          )}
        </div>
      ))}
    </section>
  )
}
