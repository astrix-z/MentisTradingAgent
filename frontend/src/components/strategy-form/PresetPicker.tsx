import { useState } from "react"

import type { PresetOut } from "@/client"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"

/** The selector's value for "no preset": the params the agent was started
 * with (or the draft built on them). */
export const CURRENT_PARAMS = "__current__"

export type PresetEntry = PresetOut & {
  /** Launchable with today's schema and lists, as the UI checks it. */
  launchable: boolean
}

type Props = {
  presets: PresetEntry[]
  selected: string
  modified: boolean
  /** Why presets cannot be shown, if they cannot. */
  loadProblem: string | null
  /** Save and Update need a draft that passed validation. */
  canSave: boolean
  busy: boolean
  onSelect: (id: string) => void
  /** Each resolves to an error message, or `null` on success. */
  onSaveAs: (name: string) => Promise<string | null>
  onUpdate: () => Promise<string | null>
  onDelete: () => Promise<string | null>
}

function presetLabel(preset: PresetEntry, modified: boolean) {
  const parts = [preset.name]
  if (preset.is_builtin) parts.push("built-in")
  let label = parts.join(" · ")
  if (!preset.launchable) label += " (invalid)"
  if (modified) label += " (modified)"
  return label
}

export function PresetPicker({
  presets,
  selected,
  modified,
  loadProblem,
  canSave,
  busy,
  onSelect,
  onSaveAs,
  onUpdate,
  onDelete,
}: Props) {
  const [saveOpen, setSaveOpen] = useState(false)
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [name, setName] = useState("")
  const [dialogError, setDialogError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const current = presets.find((preset) => preset.id === selected)
  const own = current !== undefined && !current.is_builtin

  const submitSave = async () => {
    setDialogError(null)
    const error = await onSaveAs(name)
    if (error) setDialogError(error)
    else setSaveOpen(false)
  }
  const submitDelete = async () => {
    setDialogError(null)
    const error = await onDelete()
    if (error) setDialogError(error)
    else setDeleteOpen(false)
  }
  const submitUpdate = async () => {
    setActionError(null)
    setActionError(await onUpdate())
  }

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor="strategy-preset">Preset</Label>
      <div className="flex flex-wrap gap-2">
        <Select value={selected} onValueChange={onSelect} disabled={busy}>
          <SelectTrigger id="strategy-preset" className="min-w-64">
            <SelectValue>
              {current
                ? presetLabel(current, modified)
                : "— current launch params —"}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={CURRENT_PARAMS}>
              — current launch params —
            </SelectItem>
            {presets.map((preset) => (
              <SelectItem key={preset.id} value={preset.id}>
                {presetLabel(preset, false)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Button
          type="button"
          variant="outline"
          disabled={busy || !canSave}
          onClick={() => {
            setName("")
            setDialogError(null)
            setSaveOpen(true)
          }}
        >
          Save as…
        </Button>
        {own && (
          <>
            <Button
              type="button"
              variant="outline"
              disabled={busy || !canSave}
              onClick={submitUpdate}
            >
              Update
            </Button>
            <Button
              type="button"
              variant="outline"
              disabled={busy}
              onClick={() => {
                setDialogError(null)
                setDeleteOpen(true)
              }}
            >
              Delete
            </Button>
          </>
        )}
      </div>
      {loadProblem && (
        <p role="alert" className="text-destructive text-sm">
          {loadProblem}
        </p>
      )}
      {actionError && (
        <p role="alert" className="text-destructive text-sm">
          {actionError}
        </p>
      )}

      <Dialog open={saveOpen} onOpenChange={setSaveOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Save preset</DialogTitle>
            <DialogDescription>
              Saves every setting of the form under a new name.
            </DialogDescription>
          </DialogHeader>
          <form
            className="flex flex-col gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              void submitSave()
            }}
          >
            <Label htmlFor="preset-name">Preset name</Label>
            <Input
              id="preset-name"
              value={name}
              maxLength={64}
              onChange={(event) => setName(event.target.value)}
            />
            {dialogError && (
              <p role="alert" className="text-destructive text-sm">
                {dialogError}
              </p>
            )}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setSaveOpen(false)}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={busy || name.trim() === ""}>
                Save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete preset</DialogTitle>
            <DialogDescription>
              Delete the preset {current?.name}? This cannot be undone.
            </DialogDescription>
          </DialogHeader>
          {dialogError && (
            <p role="alert" className="text-destructive text-sm">
              {dialogError}
            </p>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={busy}
              onClick={() => void submitDelete()}
            >
              Delete preset
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
