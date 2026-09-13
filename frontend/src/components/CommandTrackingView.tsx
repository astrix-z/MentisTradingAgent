import type { TrackedCommand } from "@/hooks/useCommandTracking"

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
          className="rounded-md border p-3 text-sm"
          data-testid="command"
        >
          <p>
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
            <pre className="overflow-auto">
              {JSON.stringify(result.data, null, 2)}
            </pre>
          )}
        </div>
      ))}
    </section>
  )
}
