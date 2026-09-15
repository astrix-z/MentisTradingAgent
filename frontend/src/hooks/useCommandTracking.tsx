import { useQueryClient } from "@tanstack/react-query"
import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
} from "react"

import {
  type CommandEnvelope,
  type CommandResult,
  TradingService,
} from "@/client"

export type CommandPhase =
  | "accepted"
  | "pending"
  | "succeeded"
  | "failed"
  | "timeout"
export type TrackedCommand = {
  envelope: CommandEnvelope
  phase: CommandPhase
  deadline: number
  result?: CommandResult
  pollError?: string
}

// Lack of confirmation is not proof that execution failed.
export const COMMAND_TIMEOUT_MS = 120_000

type CommandTrackingContextValue = {
  commands: TrackedCommand[]
  track: (envelope: CommandEnvelope) => void
}

const CommandTrackingContext =
  createContext<CommandTrackingContextValue | null>(null)

/**
 * Owns the active-command polling loop above the route level so navigating
 * between /strategy and /balance never drops an in-flight command_id or
 * starts a second poller for the same one.
 */
export function CommandTrackingProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [commands, setCommands] = useState<TrackedCommand[]>([])
  const active = useRef(new Map<string, TrackedCommand>())
  const track = useCallback(
    (envelope: CommandEnvelope) => {
      const command: TrackedCommand = {
        envelope,
        phase: "accepted",
        deadline: Date.now() + COMMAND_TIMEOUT_MS,
      }
      active.current.set(envelope.id, command)
      setCommands((current) => [...current, command])
      void queryClient.invalidateQueries({ queryKey: ["trading-status"] })
    },
    [queryClient],
  )

  useEffect(() => {
    let cancelled = false
    const requests = new Map<string, AbortController>()
    const update = (id: string, patch: Partial<TrackedCommand>) => {
      if (!cancelled)
        setCommands((current) =>
          current.map((c) => (c.envelope.id === id ? { ...c, ...patch } : c)),
        )
    }
    const timer = setInterval(() => {
      for (const [id, command] of active.current) {
        if (Date.now() >= command.deadline) {
          active.current.delete(id)
          requests.get(id)?.abort()
          update(id, { phase: "timeout", pollError: undefined })
          continue
        }
        if (requests.has(id)) continue
        const controller = new AbortController()
        requests.set(id, controller)
        update(id, { phase: "pending" })
        void TradingService.getCommandResult({
          query: { command_id: id },
          signal: controller.signal,
        })
          .then(({ data }) => {
            if (cancelled || !active.current.has(id)) return
            if (Date.now() >= command.deadline) {
              active.current.delete(id)
              update(id, { phase: "timeout", pollError: undefined })
              return
            }
            if (
              data?.command_id === id &&
              data.type === command.envelope.type
            ) {
              active.current.delete(id)
              update(id, {
                phase: data.ok ? "succeeded" : "failed",
                result: data,
                pollError: undefined,
              })
              void queryClient.invalidateQueries({
                queryKey: ["trading-status"],
              })
            } else {
              update(id, { phase: "pending", pollError: undefined })
            }
          })
          .catch(() => {
            if (active.current.has(id))
              update(id, {
                phase: "pending",
                pollError: "Result temporarily unavailable; retrying",
              })
          })
          .finally(() => requests.delete(id))
      }
    }, 1500)
    return () => {
      cancelled = true
      clearInterval(timer)
      for (const controller of requests.values()) controller.abort()
    }
  }, [queryClient])

  return (
    <CommandTrackingContext.Provider value={{ commands, track }}>
      {children}
    </CommandTrackingContext.Provider>
  )
}

export function useCommandTracking(): CommandTrackingContextValue {
  const context = useContext(CommandTrackingContext)
  if (!context)
    throw new Error(
      "useCommandTracking must be used within a CommandTrackingProvider",
    )
  return context
}

/**
 * The most recent tracked command matching `match`, or undefined.
 *
 * The provider keeps every tracked command so polling survives navigation, but
 * a page should show only the command it owns — and only the latest one, so a
 * repeated request replaces its section instead of piling up.
 */
export function latestTrackedCommand(
  commands: TrackedCommand[],
  match: (command: TrackedCommand) => boolean,
): TrackedCommand | undefined {
  for (let index = commands.length - 1; index >= 0; index -= 1) {
    const command = commands[index]
    if (match(command)) return command
  }
  return undefined
}
