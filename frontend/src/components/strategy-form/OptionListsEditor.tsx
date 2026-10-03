import { ChevronLeft, ChevronRight, X } from "lucide-react"
import { useState } from "react"

import { type FieldOptionsOut, type PresetOut, PresetsService } from "@/client"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { extractErrorMessage } from "@/utils"

import {
  intervalLabel,
  normalizeOption,
  optionEntryProblem,
  type StrategyField,
} from "./strategySchema"

type Props = {
  strategy: string
  fields: StrategyField[]
  /** The stored lists as `GET options/` serves them. */
  lists: Record<string, FieldOptionsOut> | undefined
  loadFailed: boolean
  presets: PresetOut[]
  /** Refresh whatever depends on the lists (the lists, preset validity). */
  onSaved: () => Promise<void>
}

type Draft = { values: string[]; entry: string; problem: string | null }

/**
 * "Option lists" (§4.5.7): what the form's selects offer. Collapsed by
 * default; each list is edited and saved on its own, whatever the state of
 * the settings form. A value the agent does not support may be kept (D3); it
 * stays visible and disabled in the select.
 */
export function OptionListsEditor({
  strategy,
  fields,
  lists,
  loadFailed,
  presets,
  onSaved,
}: Props) {
  const [open, setOpen] = useState(false)
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [saving, setSaving] = useState<string | null>(null)
  const [results, setResults] = useState<Record<string, string>>({})

  const byKey = new Map(fields.map((field) => [field.key, field]))
  const keys = Object.keys(lists ?? {}).sort((a, b) => {
    const order = fields.map((field) => field.key)
    return order.indexOf(a) - order.indexOf(b)
  })

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-4">
          <div>
            <CardTitle>Option lists</CardTitle>
            <CardDescription>
              The values the settings selects offer
            </CardDescription>
          </div>
          <Button
            type="button"
            variant="outline"
            aria-expanded={open}
            onClick={() => setOpen((value) => !value)}
          >
            {open ? "Hide option lists" : "Edit option lists"}
          </Button>
        </div>
      </CardHeader>
      {open && (
        <CardContent className="flex flex-col gap-6">
          {loadFailed && (
            <p role="alert">The option lists could not be loaded.</p>
          )}
          {!loadFailed && keys.length === 0 && (
            <p role="status">This strategy has no option lists.</p>
          )}
          {keys.map((key) => {
            const field = byKey.get(key)
            const stored = lists?.[key]
            const draft = drafts[key] ?? {
              values: stored?.options ?? [],
              entry: "",
              problem: null,
            }
            // S from the published schema; without the field, the server's
            // verdict — null when it has no catalog either, which is
            // "unknown", not "all supported".
            const supportUnknown =
              !field?.options && stored?.unsupported === null
            const isUnsupported = (value: string) =>
              field?.options
                ? !field.options.includes(value)
                : (stored?.unsupported ?? []).includes(value)
            const label = (value: string) =>
              field?.widget === "interval" ? intervalLabel(value) : value
            const edited =
              JSON.stringify(draft.values) !==
              JSON.stringify(stored?.options ?? [])
            const title = field?.title ?? key
            const removed = (stored?.options ?? []).filter(
              (value) => !draft.values.includes(value),
            )
            // Presets using a value about to go become invalid (§4.5.7).
            const affected = presets.filter((preset) =>
              removed.includes(String(preset.params[key])),
            )
            const unsupportedNow = draft.values.filter(isUnsupported)

            const update = (next: Partial<Draft>) => {
              setResults(({ [key]: _, ...rest }) => rest)
              setDrafts((current) => ({
                ...current,
                [key]: { ...draft, ...next },
              }))
            }
            const move = (index: number, delta: number) => {
              const values = [...draft.values]
              const [value] = values.splice(index, 1)
              values.splice(index + delta, 0, value)
              update({ values })
            }
            const addEntry = () => {
              const value = normalizeOption(field, draft.entry)
              const problem = optionEntryProblem(value, draft.values)
              if (problem) update({ problem })
              else
                update({
                  values: [...draft.values, value],
                  entry: "",
                  problem: null,
                })
            }
            const save = async () => {
              setSaving(key)
              try {
                await PresetsService.updateOptions({
                  path: { strategy, field: key },
                  body: { options: draft.values },
                })
                await onSaved()
                setDrafts(({ [key]: _, ...rest }) => rest)
                setResults((current) => ({ ...current, [key]: "Saved." }))
              } catch (error) {
                setResults((current) => ({
                  ...current,
                  [key]: extractErrorMessage(error as Error),
                }))
              } finally {
                setSaving(null)
              }
            }

            return (
              <section
                key={key}
                aria-label={`${title} options`}
                className="flex flex-col gap-2"
              >
                <h3 className="font-medium text-sm">{title}</h3>
                <ul className="flex flex-wrap gap-2">
                  {draft.values.map((value, index) => (
                    <li key={value}>
                      <Badge
                        variant={isUnsupported(value) ? "outline" : "secondary"}
                        className={cn(
                          "gap-1 py-1",
                          isUnsupported(value) &&
                            "border-amber-500 text-amber-700 dark:text-amber-400",
                        )}
                        title={
                          isUnsupported(value)
                            ? "Not supported by the agent"
                            : value
                        }
                        data-option={value}
                        data-unsupported={isUnsupported(value) || undefined}
                      >
                        <button
                          type="button"
                          aria-label={`Move ${label(value)} earlier`}
                          disabled={index === 0}
                          onClick={() => move(index, -1)}
                        >
                          <ChevronLeft className="size-3" />
                        </button>
                        {label(value)}
                        <button
                          type="button"
                          aria-label={`Move ${label(value)} later`}
                          disabled={index === draft.values.length - 1}
                          onClick={() => move(index, 1)}
                        >
                          <ChevronRight className="size-3" />
                        </button>
                        <button
                          type="button"
                          aria-label={`Remove ${label(value)}`}
                          onClick={() =>
                            update({
                              values: draft.values.filter(
                                (_, position) => position !== index,
                              ),
                            })
                          }
                        >
                          <X className="size-3" />
                        </button>
                      </Badge>
                    </li>
                  ))}
                </ul>
                <form
                  className="flex gap-2"
                  onSubmit={(event) => {
                    event.preventDefault()
                    addEntry()
                  }}
                >
                  <Input
                    aria-label={`Add to ${title}`}
                    placeholder={
                      field?.widget === "interval" ? "H1, 1h, D1…" : "Value"
                    }
                    value={draft.entry}
                    onChange={(event) =>
                      update({ entry: event.target.value, problem: null })
                    }
                    className="max-w-48"
                  />
                  <Button type="submit" variant="outline">
                    Add
                  </Button>
                  <Button
                    type="button"
                    disabled={
                      !edited || saving !== null || draft.values.length === 0
                    }
                    onClick={() => void save()}
                  >
                    {saving === key ? "Saving…" : `Save ${title}`}
                  </Button>
                  {edited && (
                    <Button
                      type="button"
                      variant="ghost"
                      onClick={() => setDrafts(({ [key]: _, ...rest }) => rest)}
                    >
                      Discard
                    </Button>
                  )}
                </form>
                {draft.problem && (
                  <p role="alert" className="text-destructive text-sm">
                    {draft.problem}
                  </p>
                )}
                {supportUnknown && draft.values.length > 0 && (
                  <p
                    role="status"
                    className="text-amber-700 text-sm dark:text-amber-400"
                  >
                    The agent's catalog is unavailable, so it is not known which
                    of these values the agent supports.
                  </p>
                )}
                {unsupportedNow.length > 0 && (
                  <p className="text-amber-700 text-sm dark:text-amber-400">
                    Not supported by the agent:{" "}
                    {unsupportedNow.map(label).join(", ")}. They are kept in the
                    list but cannot be selected.
                  </p>
                )}
                {edited && draft.values.length === 0 && (
                  <p role="alert" className="text-destructive text-sm">
                    A list cannot be empty.
                  </p>
                )}
                {edited && affected.length > 0 && (
                  <p role="alert" className="text-amber-700 text-sm">
                    Presets using a removed value will become invalid:{" "}
                    {affected.map((preset) => preset.name).join(", ")}.
                  </p>
                )}
                {results[key] && (
                  <p role="status" className="text-sm">
                    {results[key]}
                  </p>
                )}
              </section>
            )
          })}
        </CardContent>
      )}
    </Card>
  )
}
