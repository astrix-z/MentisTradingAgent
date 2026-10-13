import { useMutation } from "@tanstack/react-query"
import { useState } from "react"

import { TradingService, type TradingStatus } from "@/client"
import { CommandTrackingView } from "@/components/CommandTrackingView"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { LoadingButton } from "@/components/ui/loading-button"
import {
  latestTrackedCommand,
  useCommandTracking,
} from "@/hooks/useCommandTracking"
import useCustomToast from "@/hooks/useCustomToast"
import { handleError } from "@/utils"

// Client id of an entry attempt (agent attempts.client_id, role "e"); the
// backend accepts release_entry for these only.
const ENTRY_CLIENT_ID = /mta-[0-9a-f]{24}-e[1-9][0-9]{0,5}/
const ENTRY_CLIENT_ID_EXACT = new RegExp(`^${ENTRY_CLIENT_ID.source}$`)

/** The entry client id named by the agent's `unknown` alert, if any. */
export function unknownEntryId(status?: TradingStatus): string | undefined {
  const alert = status?.actual?.execution?.alerts?.unknown
  return alert?.match(ENTRY_CLIENT_ID)?.[0]
}

/**
 * The `release_entry` command (runbook §1): release an UNKNOWN entry whose
 * order the agent proves itself was never placed. A refusal comes back as the
 * command's failure message; nothing changes then.
 */
export function ReleaseEntry({ status }: { status?: TradingStatus }) {
  const alert = status?.actual?.execution?.alerts?.unknown
  const suggested = unknownEntryId(status)
  const [typed, setTyped] = useState<string>()
  const [open, setOpen] = useState(false)
  const clientId = (typed ?? suggested ?? "").trim()
  const valid = ENTRY_CLIENT_ID_EXACT.test(clientId)
  const { commands, track } = useCommandTracking()
  const { showErrorToast } = useCustomToast()
  const latest = latestTrackedCommand(
    commands,
    (command) => command.envelope.type === "release_entry",
  )
  const mutation = useMutation({
    mutationFn: (client_order_id: string) =>
      TradingService.issueCommand({
        body: { type: "release_entry", payload: { client_order_id } },
      }),
    onSuccess: ({ data }) => {
      track(data)
      setOpen(false)
      setTyped(undefined)
    },
    onError: handleError.bind(showErrorToast),
  })

  return (
    <section className="space-y-3 text-sm" aria-label="Release entry">
      {alert ? (
        <p role="alert">Unresolved entry: {alert}</p>
      ) : (
        <p className="text-muted-foreground">
          No entry with an unknown outcome.
        </p>
      )}
      <p className="text-muted-foreground">
        For an entry held UNKNOWN by a BUY the agent cannot name, such as a
        manual purchase. The agent releases it only if it proves the order was
        never placed; otherwise it refuses and nothing changes.
      </p>
      <div className="flex flex-col gap-2">
        <Label htmlFor="release-entry-id">Entry client order id</Label>
        <Input
          id="release-entry-id"
          className="font-mono"
          placeholder="mta-…-e1"
          value={clientId}
          onChange={(event) => setTyped(event.target.value)}
          aria-invalid={clientId !== "" && !valid}
        />
        {clientId !== "" && !valid && (
          <p className="text-destructive">
            Not an entry client id (mta-&lt;24 hex&gt;-e&lt;n&gt;).
          </p>
        )}
      </div>
      <Dialog open={open} onOpenChange={setOpen}>
        <Button
          type="button"
          variant="outline"
          disabled={!valid || mutation.isPending}
          onClick={() => setOpen(true)}
        >
          Release entry
        </Button>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Release entry</DialogTitle>
            <DialogDescription>
              Release <span className="font-mono">{clientId}</span>? Check first
              that the order is not in the Binance order history (runbook §1).
              The agent checks again and refuses if the order may exist.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter className="mt-4">
            <DialogClose asChild>
              <Button variant="outline" disabled={mutation.isPending}>
                Cancel
              </Button>
            </DialogClose>
            <LoadingButton
              type="button"
              loading={mutation.isPending}
              onClick={() => mutation.mutate(clientId)}
            >
              Release
            </LoadingButton>
          </DialogFooter>
        </DialogContent>
      </Dialog>
      {latest && <CommandTrackingView commands={[latest]} />}
    </section>
  )
}
