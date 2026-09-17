import type { TradingStatus } from "@/client"

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
      {actual?.message && <p role="alert">{actual.message}</p>}
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
            {desired.symbols?.length ?? 0})
            {stale ? " (stale)" : ""}:
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
        <div>
          <h2 className="font-semibold">
            Heartbeat balances —{" "}
            {actual.network?.toUpperCase() ?? "UNKNOWN NETWORK"}
            {stale ? " (stale)" : ""}
          </h2>
          <pre>{JSON.stringify(actual.balances, null, 2)}</pre>
        </div>
      )}
    </section>
  )
}
