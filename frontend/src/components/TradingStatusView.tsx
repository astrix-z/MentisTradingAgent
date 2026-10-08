import type { TradingStatus } from "@/client"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"

export function TradingStatusView({
  status,
  error,
  balances = false,
}: {
  status?: TradingStatus
  error?: boolean
  /**
   * Heartbeat balances belong to the Balance page. Off by default so account
   * amounts cannot leak onto a page that is about configuration and control.
   */
  balances?: boolean
}) {
  if (!status) return <p>{error ? "Status unavailable" : "Loading status…"}</p>
  const { desired, actual, online } = status
  const stale = !online || error
  const mismatch =
    !actual ||
    desired.running !== (actual.state === "running") ||
    (desired.running &&
      (desired.network !== actual.network ||
        desired.strategy !== actual.strategy))
  return (
    <section className="space-y-3 text-sm" aria-label="Trading status">
      <h2 className="font-semibold">Actual agent state</h2>
      <p>
        {stale
          ? "Offline / stale — last heartbeat, not a confirmed current run"
          : "Online"}
      </p>
      <p>
        Actual state: {actual?.state ?? "unknown"}
        {stale && actual ? " (stale)" : ""}
      </p>
      <p>Actual network: {actual?.network?.toUpperCase() ?? "unknown"}</p>
      <p>Actual strategy: {actual?.strategy ?? "unknown"}</p>
      {actual && <p>Heartbeat: {actual.updated_at}</p>}
      {actual?.message && (
        // One entry per line, and a bounded box: dozens of execution
        // divergences must scroll here, not stretch the page over screens.
        // wrap-anywhere, unlike break-words, also shrinks the min-content
        // width, so an unbroken id or URL cannot widen the grid on a phone.
        <p
          role="alert"
          data-testid="agent-message"
          className="max-h-48 overflow-y-auto whitespace-pre-wrap wrap-anywhere rounded-md border p-2 font-mono text-xs"
        >
          {actual.message.split("; ").join("\n")}
        </p>
      )}
      <h2 className="font-semibold">
        Desired configuration (not execution confirmation)
      </h2>
      <p>Desired state: {desired.running ? "running" : "stopped"}</p>
      <p>Desired network: {desired.network?.toUpperCase()}</p>
      <p>Desired strategy: {desired.strategy ?? "none"}</p>
      <details>
        <summary className="cursor-pointer select-none">
          Desired symbols ({desired.symbols?.length ?? 0})
        </summary>
        <p className="font-mono text-xs">
          {desired.symbols?.join(", ") || "none"}
        </p>
      </details>
      {actual?.active_symbols != null && (
        <div>
          <p>
            Screened symbols ({actual.active_symbols.length} of{" "}
            {desired.symbols?.length ?? 0}){stale ? " (stale)" : ""}:
          </p>
          <p className="font-mono text-xs">
            {actual.active_symbols.join(", ") || "none"}
          </p>
        </div>
      )}
      <p>Revision: {desired.revision}</p>
      {(mismatch || stale) && (
        <p role="status">
          Desired / actual not confirmed; pending or rejected command, or stale
          heartbeat.
        </p>
      )}
      {balances && actual?.balances && (
        // Collapsed by default: amounts are shown only on an explicit click.
        <details>
          <summary className="cursor-pointer select-none font-semibold">
            Heartbeat balances —{" "}
            {actual.network?.toUpperCase() ?? "UNKNOWN NETWORK"}
            {stale ? " (stale)" : ""} ({Object.keys(actual.balances).length})
          </summary>
          <Table aria-label="Heartbeat balances" className="mt-2">
            <TableHeader>
              <TableRow>
                <TableHead>Asset</TableHead>
                <TableHead className="text-right">Amount</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {Object.entries(actual.balances).map(([asset, amount]) => (
                <TableRow key={asset}>
                  <TableCell className="font-medium">{asset}</TableCell>
                  <TableCell className="text-right font-mono">
                    {amount}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </details>
      )}
    </section>
  )
}
