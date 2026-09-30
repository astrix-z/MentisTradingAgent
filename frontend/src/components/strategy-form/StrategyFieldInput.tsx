import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { cn } from "@/lib/utils"

import {
  type FieldValue,
  type HistoryHint,
  intervalLabel,
  type StrategyField,
  type ValidationIssue,
} from "./strategySchema"

type Props = {
  field: StrategyField
  value: FieldValue
  /** The field's option list from the database; `undefined` — no list, S. */
  optionList?: string[]
  issue?: ValidationIssue
  history?: HistoryHint
  disabled: boolean
  onChange: (value: FieldValue) => void
}

type Choice = { value: string; label: string; disabled: boolean }

/** The options a `Select` offers: the list (or S without one), an option the
 * agent does not support shown but disabled, and a current value outside the
 * list kept visible so its error has something to point at (§4.5.2). */
function choicesFor(field: StrategyField, value: string, list?: string[]) {
  const supported = field.options ?? []
  const label = (option: string) =>
    field.widget === "interval" ? intervalLabel(option) : option
  const choices: Choice[] = (list ?? supported).map((option) => {
    const known = supported.includes(option)
    return {
      value: option,
      label: known
        ? label(option)
        : `${label(option)} — not supported by the agent`,
      disabled: !known,
    }
  })
  if (value !== "" && !choices.some((choice) => choice.value === value))
    choices.push({
      value,
      label: `${label(value)} — not in the list`,
      disabled: true,
    })
  return choices
}

function formatDays(days: number): string {
  if (days >= 1) return `${Math.round(days * 10) / 10} days`
  return `${Math.round(days * 24 * 10) / 10} hours`
}

export function StrategyFieldInput({
  field,
  value,
  optionList,
  issue,
  history,
  disabled,
  onChange,
}: Props) {
  const invalid = issue !== undefined
  const text = Array.isArray(value) ? value.join(", ") : value

  let control: React.ReactNode
  if (field.kind === "const") {
    control = <Input id={field.key} value={text} readOnly disabled />
  } else if (field.kind === "enum") {
    control = (
      <Select value={text} onValueChange={onChange} disabled={disabled}>
        <SelectTrigger id={field.key} aria-invalid={invalid} className="w-full">
          <SelectValue placeholder="Choose…" />
        </SelectTrigger>
        <SelectContent>
          {choicesFor(field, text, optionList).map((choice) => (
            <SelectItem
              key={choice.value}
              value={choice.value}
              disabled={choice.disabled}
            >
              {choice.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    )
  } else if (field.kind === "array") {
    const items = Array.isArray(value) ? value : [value]
    // One input per element; a saved list with more elements than the schema
    // allows shows them all, and its error says why.
    const count = Math.max(field.maxItems ?? items.length, items.length)
    const setItem = (index: number, next: string) => {
      const updated = Array.from({ length: count }, (_, i) => items[i] ?? "")
      updated[index] = next
      // Emptied extra elements beyond the schema's length disappear.
      const keep = field.maxItems ?? updated.length
      while (updated.length > keep && updated.at(-1) === "") updated.pop()
      onChange(updated)
    }
    control = (
      <div className="flex gap-2">
        {Array.from({ length: count }, (_, index) => (
          <Input
            key={index}
            id={index === 0 ? field.key : undefined}
            aria-label={`${field.title} #${index + 1}`}
            value={items[index] ?? ""}
            disabled={disabled}
            aria-invalid={invalid}
            inputMode="numeric"
            onChange={(event) => setItem(index, event.target.value)}
          />
        ))}
      </div>
    )
  } else {
    control = (
      <Input
        id={field.key}
        value={text}
        disabled={disabled}
        aria-invalid={invalid}
        inputMode={field.kind === "text" ? undefined : "decimal"}
        onChange={(event) => onChange(event.target.value)}
      />
    )
  }

  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor={field.key}>{field.title}</Label>
      {control}
      {issue && (
        <p className="text-destructive text-xs" data-field-error={field.key}>
          {issue.message}
        </p>
      )}
      {history && (
        <p
          className={cn(
            "text-xs",
            history.tooLong
              ? "text-amber-600 dark:text-amber-400"
              : "text-muted-foreground",
          )}
          data-history={field.key}
        >
          History: {formatDays(history.days)}
          {history.tooLong &&
            " — more than a year; the exchange may not have it for every symbol"}
        </p>
      )}
    </div>
  )
}
