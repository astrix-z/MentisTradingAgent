// Renders and validates strategy settings straight from the JSON Schema and
// constraint AST the agent publishes (`StrategyDescription.settings_schema`
// and `.constraints`). Nothing here names a specific strategy: labels come
// from each property's `title`, values from its `default`, and cross-field
// rules are interpreted from the declarative constraint expressions.
//
// The checks follow the backend pipeline (plan §4.3.5, §4.5.3–§4.5.5) and may
// be stricter than it, never softer: fields (type, domain, bounds, supported
// values) stop the check; the published constraints and the option lists are
// then checked together. All arithmetic is exact (`exactDecimal.ts`).

import {
  add,
  compare,
  type Decimal,
  decimalFromNumber,
  formatDecimal,
  INTEGER_TEXT,
  max,
  mul,
  parseDecimal,
  sub,
} from "./exactDecimal"

export type StrategyBounds = {
  gt?: number
  gte?: number
  lt?: number
  lte?: number
}

export type StrategyFieldKind =
  | "const"
  | "enum"
  | "text"
  | "integer"
  | "decimal"
  | "array"

export type StrategyField = {
  key: string
  /** Ready-to-show label, taken verbatim from the schema's `title`. */
  title: string
  kind: StrategyFieldKind
  /** Default as the form holds it: text, or one text per array element. */
  defaultValue: FieldValue
  /** The pinned value of a `const` field, sent as published. */
  constValue?: unknown
  /** Values the agent supports (S) for `enum` fields. */
  options?: string[]
  /** `ui.widget` of the property, e.g. `interval`. */
  widget?: string
  /** Numeric bounds for scalar and array-item fields. */
  bounds?: StrategyBounds
  /** How a decimal travels: a string keeps precision, a plain `number`
   * property only accepts a JSON number (§4.3.5). */
  wire?: "string" | "number"
  minItems?: number
  maxItems?: number
  /** Element type for `array` fields. */
  itemKind?: "integer" | "number"
}

/** Text of a scalar field, one text per element of an array field. */
export type FieldValue = string | string[]
export type FieldValues = Record<string, FieldValue>
export type Lookups = Record<string, Record<string, number>>
/** A constraint rule exactly as published; keys are validated at runtime. */
export type ConstraintRule = Record<string, unknown>
/** Option lists kept in the database, by field; a field without a row has no
 * list and is checked against S only (§4.3.5 step 5). */
export type OptionLists = Record<string, string[]>

/** The UI's own error types; `error_map.ui` of the shared fixture maps them
 * to the categories every layer reports (§6). */
export type IssueType =
  | "required"
  | "integer_format"
  | "unsafe_integer"
  | "decimal_format"
  | "item_count"
  | "too_large"
  | "bound"
  | "unsupported"
  | "option"
  | "constraint"

export type ValidationIssue = {
  type: IssueType
  /** The field, or `null` for a cross-field rule. */
  field: string | null
  message: string
}

export type ValidationResult =
  | { status: "valid"; params: Record<string, unknown> }
  | { status: "invalid"; issues: ValidationIssue[] }
  /** The context cannot be checked (fail closed): Start and Save stay off. */
  | { status: "unavailable"; reason: string }

const UNABLE_TO_EVALUATE =
  "Settings could not be checked against the agent's constraints; Start is blocked."

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
}

function readNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined
}

function boundsFrom(
  schema: Record<string, unknown> | undefined,
): StrategyBounds {
  const bounds: StrategyBounds = {}
  if (!schema) return bounds
  const gt = readNumber(schema.exclusiveMinimum)
  if (gt !== undefined) bounds.gt = gt
  const gte = readNumber(schema.minimum)
  if (gte !== undefined) bounds.gte = gte
  const lt = readNumber(schema.exclusiveMaximum)
  if (lt !== undefined) bounds.lt = lt
  const lte = readNumber(schema.maximum)
  if (lte !== undefined) bounds.lte = lte
  return bounds
}

function humanize(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim()
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

function formatScalar(value: unknown): string {
  if (value === undefined || value === null) return ""
  return String(value)
}

/** Serialize a param value for display (arrays join with ", "). */
export function formatParamValue(value: unknown): string {
  if (Array.isArray(value)) return value.map(formatScalar).join(", ")
  return formatScalar(value)
}

/** A param value as the form holds it for `field`. */
function toFieldValue(field: StrategyField, value: unknown): FieldValue {
  if (field.kind === "array")
    return Array.isArray(value)
      ? value.map(formatScalar)
      : [formatScalar(value)]
  // A list in a scalar field is kept visible, and fails its check.
  return formatParamValue(value)
}

/** Binance interval code as the UI shows it: `1w` → `W1`, `15m` → `M15`.
 * Anything else (`1M`, `custom`) is shown as is. */
export function intervalLabel(code: string): string {
  const match = /^(\d+)([smhdw])$/.exec(code)
  if (!match) return code
  return `${match[2].toUpperCase()}${match[1]}`
}

/** Parse a settings JSON Schema into the fields the form renders. */
export function parseSettingsSchema(schema: unknown): StrategyField[] {
  if (!isRecord(schema)) return []
  const properties = schema.properties
  if (!isRecord(properties)) return []

  const fields: StrategyField[] = []
  for (const [key, raw] of Object.entries(properties)) {
    const prop = isRecord(raw) ? raw : {}
    const title = typeof prop.title === "string" ? prop.title : humanize(key)
    const ui = isRecord(prop.ui) ? prop.ui : {}
    const widget = typeof ui.widget === "string" ? ui.widget : undefined

    // Fixed value: the schema pins it with `const`.
    if (prop.const !== undefined) {
      fields.push({
        key,
        title,
        kind: "const",
        constValue: prop.const,
        defaultValue: formatParamValue(prop.const),
      })
      continue
    }

    // Choice of allowed strings (includes the interval widget).
    if (Array.isArray(prop.enum)) {
      const options = prop.enum.map((option) => String(option))
      fields.push({
        key,
        title,
        kind: "enum",
        options,
        widget,
        // No declared default: nothing is chosen for the user. A preset that
        // lacks the value stays missing and fails as required.
        defaultValue: prop.default !== undefined ? String(prop.default) : "",
      })
      continue
    }

    // Fixed-length numeric list, one input per element.
    if (prop.type === "array") {
      const prefixItems = Array.isArray(prop.prefixItems)
        ? prop.prefixItems
        : []
      const item = isRecord(prefixItems[0]) ? prefixItems[0] : undefined
      const count = prefixItems.length
      const minItems = readNumber(prop.minItems) ?? count
      const maxItems = readNumber(prop.maxItems) ?? count
      const defaults = Array.isArray(prop.default)
        ? prop.default.map(formatScalar)
        : Array.from({ length: minItems }, () => "")
      fields.push({
        key,
        title,
        kind: "array",
        defaultValue: defaults,
        minItems,
        maxItems,
        itemKind: item?.type === "number" ? "number" : "integer",
        bounds: boundsFrom(item),
      })
      continue
    }

    // Decimal: `anyOf` a bounded number with a decimal string. The value is
    // kept as text so precision survives the round-trip.
    if (Array.isArray(prop.anyOf)) {
      const numberBranch = prop.anyOf.find(
        (branch) => isRecord(branch) && branch.type === "number",
      )
      if (isRecord(numberBranch)) {
        fields.push({
          key,
          title,
          kind: "decimal",
          wire: "string",
          defaultValue: formatParamValue(prop.default),
          bounds: boundsFrom(numberBranch),
        })
        continue
      }
    }

    if (prop.type === "integer") {
      fields.push({
        key,
        title,
        kind: "integer",
        defaultValue: formatParamValue(prop.default),
        bounds: boundsFrom(prop),
      })
      continue
    }

    if (prop.type === "number") {
      fields.push({
        key,
        title,
        kind: "decimal",
        wire: "number",
        defaultValue: formatParamValue(prop.default),
        bounds: boundsFrom(prop),
      })
      continue
    }

    fields.push({
      key,
      title,
      kind: "text",
      widget,
      defaultValue: formatParamValue(prop.default),
    })
  }
  return fields
}

/** Initial draft: every field at the default the schema declares. */
export function initialFormValues(fields: StrategyField[]): FieldValues {
  const values: FieldValues = {}
  for (const field of fields) values[field.key] = field.defaultValue
  return values
}

/**
 * The form for a set of params (the agent's saved desired params, or a
 * preset) on top of the schema defaults. `legacyKeys` (old name -> new name,
 * from the catalog) reads a value saved under a renamed parameter's old
 * name; the new name wins if both are saved. Keys the schema does not name
 * are dropped.
 */
export function seedFormValues(
  fields: StrategyField[],
  params: Record<string, unknown> | undefined,
  legacyKeys: Record<string, string> = {},
): FieldValues {
  const values = initialFormValues(fields)
  if (!params) return values
  const current: Record<string, unknown> = {}
  for (const [oldKey, newKey] of Object.entries(legacyKeys)) {
    if (params[oldKey] !== undefined) current[newKey] = params[oldKey]
  }
  Object.assign(current, params)
  for (const field of fields) {
    const raw = current[field.key]
    if (raw === undefined || field.kind === "const") continue
    values[field.key] = toFieldValue(field, raw)
  }
  return values
}

/**
 * Carry a dirty draft over to a changed schema (§4.6): kept fields keep their
 * text, a value under a renamed key moves to the new name, keys the schema no
 * longer has are dropped, new fields start at their default.
 */
export function migrateDraft(
  fields: StrategyField[],
  draft: FieldValues,
  legacyKeys: Record<string, string> = {},
): FieldValues {
  const values = initialFormValues(fields)
  const byNewKey: Record<string, FieldValue> = {}
  for (const [oldKey, newKey] of Object.entries(legacyKeys)) {
    if (draft[oldKey] !== undefined) byNewKey[newKey] = draft[oldKey]
  }
  for (const field of fields) {
    if (field.kind === "const") continue
    const kept = draft[field.key] ?? byNewKey[field.key]
    if (kept === undefined) continue
    if (field.kind === "array")
      values[field.key] = Array.isArray(kept) ? kept : [kept]
    else values[field.key] = Array.isArray(kept) ? kept.join(", ") : kept
  }
  return values
}

// --- Field checks -----------------------------------------------------------

type FieldCheck = { value: unknown } | { issue: IssueType; message: string }

function boundIssue(
  title: string,
  value: Decimal,
  bounds: StrategyBounds,
): string | undefined {
  const checks: [number | undefined, (c: number) => boolean, string][] = [
    [bounds.gt, (c) => c > 0, "greater than"],
    [bounds.gte, (c) => c >= 0, "greater than or equal to"],
    [bounds.lt, (c) => c < 0, "less than"],
    [bounds.lte, (c) => c <= 0, "less than or equal to"],
  ]
  for (const [bound, passes, words] of checks) {
    if (bound === undefined) continue
    const limit = decimalFromNumber(bound)
    if (limit && !passes(compare(value, limit)))
      return `${title} must be ${words} ${bound}`
  }
  return undefined
}

/** An integer: sign and digits only, a safe JS integer (stricter than the
 * backend, never softer), inside the bounds. */
function checkInteger(
  title: string,
  text: string,
  bounds: StrategyBounds,
): FieldCheck {
  if (text === "") return { issue: "required", message: `${title} is required` }
  if (!INTEGER_TEXT.test(text))
    return {
      issue: "integer_format",
      message: `${title} must be a whole number`,
    }
  const value = Number(text)
  if (!Number.isSafeInteger(value))
    return {
      issue: "unsafe_integer",
      message: `${title} is too large to check safely`,
    }
  const exact = decimalFromNumber(value)
  const bound = exact && boundIssue(title, exact, bounds)
  if (bound) return { issue: "bound", message: bound }
  return { value }
}

/** A decimal: the backend grammar, the numeric domain before the bounds
 * (one `too_large` per field), exact bounds. */
function checkDecimal(
  title: string,
  text: string,
  bounds: StrategyBounds,
  wire: "string" | "number",
): FieldCheck {
  if (text === "") return { issue: "required", message: `${title} is required` }
  const parsed = parseDecimal(text)
  if (parsed.kind === "format")
    return { issue: "decimal_format", message: `${title} must be a number` }
  if (parsed.kind === "too_large")
    return {
      issue: "too_large",
      message: `${title} is outside the supported range (at most 64 significant digits, exponent within ±64)`,
    }
  const bound = boundIssue(title, parsed.value, bounds)
  if (bound) return { issue: "bound", message: bound }
  // Decimals stay strings so precision is preserved on the wire.
  return { value: wire === "number" ? Number(text) : text }
}

function checkArray(field: StrategyField, raw: FieldValue): FieldCheck {
  const items = Array.isArray(raw) ? raw : [raw]
  const min = field.minItems ?? items.length
  const maxCount = field.maxItems ?? items.length
  if (items.length < min || items.length > maxCount) {
    const expected =
      min === maxCount ? `exactly ${min}` : `between ${min} and ${maxCount}`
    return {
      issue: "item_count",
      message: `${field.title} must have ${expected} values`,
    }
  }
  const values: unknown[] = []
  for (const [index, text] of items.entries()) {
    const title = `${field.title} #${index + 1}`
    const check =
      field.itemKind === "number"
        ? checkDecimal(title, text, field.bounds ?? {}, "number")
        : checkInteger(title, text, field.bounds ?? {})
    if ("issue" in check) return check
    values.push(check.value)
  }
  return { value: values }
}

function checkField(field: StrategyField, raw: FieldValue): FieldCheck {
  if (field.kind === "const") return { value: field.constValue }
  if (field.kind === "array") return checkArray(field, raw)
  const text = Array.isArray(raw) ? raw.join(", ") : raw
  if (field.kind === "integer")
    return checkInteger(field.title, text, field.bounds ?? {})
  if (field.kind === "decimal")
    return checkDecimal(
      field.title,
      text,
      field.bounds ?? {},
      field.wire ?? "string",
    )
  if (field.kind === "enum") {
    if (text === "")
      return { issue: "required", message: `${field.title} is required` }
    // Outside S stops this field before the option lists (§4.5.3).
    if (!(field.options ?? []).includes(text))
      return {
        issue: "unsupported",
        message: `${field.title}: the agent does not support ${text}`,
      }
    return { value: text }
  }
  const trimmed = text.trim()
  if (trimmed === "")
    return { issue: "required", message: `${field.title} is required` }
  return { value: trimmed }
}

// --- Constraints --------------------------------------------------------------

function resolveField(name: unknown, values: Record<string, unknown>): unknown {
  if (typeof name === "string") return values[name]
  if (
    Array.isArray(name) &&
    typeof name[0] === "string" &&
    typeof name[1] === "number"
  ) {
    const container = values[name[0]]
    if (!Array.isArray(container)) return undefined
    return container[name[1]]
  }
  return undefined
}

function toDecimal(value: unknown): Decimal | undefined {
  if (typeof value === "number") return decimalFromNumber(value)
  if (typeof value === "string") {
    const parsed = parseDecimal(value)
    return parsed.kind === "ok" ? parsed.value : undefined
  }
  return undefined
}

const OPERATORS: Record<string, (a: Decimal, b: Decimal) => Decimal> = {
  add,
  sub,
  mul,
  max,
}

/** Evaluate one constraint expression exactly, or `undefined` if it cannot
 * be evaluated (unknown operator, missing lookup key, non-number, ...). */
function evaluateExpr(
  expr: unknown,
  values: Record<string, unknown>,
  lookups: Lookups,
): Decimal | undefined {
  if (!isRecord(expr)) return undefined

  if ("const" in expr) return toDecimal(readNumber(expr.const))

  if ("lookup" in expr) {
    const tableName = expr.lookup
    if (typeof tableName !== "string") return undefined
    const raw = resolveField(expr.field, values)
    if (raw === undefined) return undefined
    const table = lookups[tableName]
    if (!table || !Object.hasOwn(table, String(raw))) return undefined
    return toDecimal(readNumber(table[String(raw)]))
  }

  if ("field" in expr) return toDecimal(resolveField(expr.field, values))

  if ("op" in expr) {
    const args = expr.args
    const operator =
      typeof expr.op === "string" && Object.hasOwn(OPERATORS, expr.op)
        ? OPERATORS[expr.op]
        : undefined
    if (!operator || !Array.isArray(args) || args.length === 0) return undefined
    const numbers: Decimal[] = []
    for (const arg of args) {
      const value = evaluateExpr(arg, values, lookups)
      if (value === undefined) return undefined
      numbers.push(value)
    }
    return numbers.slice(1).reduce(operator, numbers[0])
  }

  return undefined
}

const COMPARISONS: Record<string, (c: number) => boolean> = {
  lt: (c) => c < 0,
  lte: (c) => c <= 0,
  gt: (c) => c > 0,
  gte: (c) => c >= 0,
  eq: (c) => c === 0,
  ne: (c) => c !== 0,
}

function substituteMessage(message: string, lhs: Decimal, rhs: Decimal) {
  return message
    .replace(/\{lhs\}/g, formatDecimal(lhs))
    .replace(/\{rhs\}/g, formatDecimal(rhs))
}

function runConstraints(
  constraints: ConstraintRule[],
  values: Record<string, unknown>,
  lookups: Lookups,
): { messages: string[]; evaluable: boolean } {
  const messages = new Set<string>()
  for (const rule of constraints) {
    const cmp = rule.cmp
    const message = rule.message
    if (
      typeof cmp !== "string" ||
      !Object.hasOwn(COMPARISONS, cmp) ||
      typeof message !== "string"
    )
      return { messages: [...messages], evaluable: false }
    const lhs = evaluateExpr(rule.lhs, values, lookups)
    const rhs = evaluateExpr(rule.rhs, values, lookups)
    if (lhs === undefined || rhs === undefined)
      return { messages: [...messages], evaluable: false }
    if (!COMPARISONS[cmp](compare(lhs, rhs)))
      messages.add(substituteMessage(message, lhs, rhs))
  }
  return { messages: [...messages], evaluable: true }
}

/**
 * Validate the whole draft against the field schemas, the supported values
 * (S, the schema enums), the published constraints and the option lists.
 * Params are exactly the schema's fields (const ones included): a key the
 * schema does not name — an old parameter name or anything else left in the
 * saved desired params — is never sent, since the backend rejects it.
 */
export function validateSettings(options: {
  fields: StrategyField[]
  values: FieldValues
  lookups: Lookups
  constraints: ConstraintRule[]
  optionLists: OptionLists
}): ValidationResult {
  const { fields, values, lookups, constraints, optionLists } = options
  const issues: ValidationIssue[] = []
  const parsed: Record<string, unknown> = {}
  for (const field of fields) {
    const check = checkField(field, values[field.key] ?? field.defaultValue)
    if ("issue" in check)
      issues.push({
        type: check.issue,
        field: field.key,
        message: check.message,
      })
    else parsed[field.key] = check.value
  }
  if (issues.length > 0) return { status: "invalid", issues }

  const constraintResult = runConstraints(constraints, parsed, lookups)
  if (!constraintResult.evaluable)
    return { status: "unavailable", reason: UNABLE_TO_EVALUATE }
  for (const message of constraintResult.messages)
    issues.push({ type: "constraint", field: null, message })

  for (const field of fields) {
    const list = optionLists[field.key]
    const value = parsed[field.key]
    if (field.kind !== "enum" || !list || list.includes(String(value))) continue
    issues.push({
      type: "option",
      field: field.key,
      message: `${field.title}: ${String(value)} is not in the option list`,
    })
  }
  if (issues.length > 0) return { status: "invalid", issues }
  return { status: "valid", params: parsed }
}

// --- Comparison and hints -------------------------------------------------------

/** A value normalized for comparison: numbers by exact value (`1.0` = `1`),
 * anything unparseable by its text. */
function canonical(field: StrategyField, value: FieldValue): string {
  const one = (text: string) => {
    if (
      field.kind !== "integer" &&
      field.kind !== "decimal" &&
      field.kind !== "array"
    )
      return text
    const parsed = parseDecimal(text)
    return parsed.kind === "ok" ? formatDecimal(parsed.value) : `raw:${text}`
  }
  return Array.isArray(value) ? JSON.stringify(value.map(one)) : one(value)
}

/** Whether the draft holds exactly these params in every non-const field. */
export function sameParams(
  fields: StrategyField[],
  values: FieldValues,
  params: Record<string, unknown>,
  legacyKeys: Record<string, string> = {},
): boolean {
  const other = seedFormValues(fields, params, legacyKeys)
  for (const field of fields) {
    if (field.kind === "const") continue
    // A field the params lack is filled from the default by the seed, but
    // the params still differ from what the form would send (§4.3.3).
    const present =
      params[field.key] !== undefined ||
      Object.entries(legacyKeys).some(
        ([oldKey, newKey]) =>
          newKey === field.key && params[oldKey] !== undefined,
      )
    if (!present) return false
    const mine = values[field.key] ?? field.defaultValue
    if (canonical(field, mine) !== canonical(field, other[field.key]))
      return false
  }
  return true
}

/** The field that sets how many bars the agent loads per timeframe. */
export const HISTORY_BARS_FIELD = "kline_window"
/** D4: history longer than this is warned about, never blocked. */
const HISTORY_WARNING_MS = 365 * 24 * 60 * 60 * 1000

export type HistoryHint = {
  field: string
  interval: string
  days: number
  tooLong: boolean
  /** The smallest window the published constraints allow for the current
   * values, and the history it takes on this interval; shown all the time,
   * not only once the window is already too small. */
  minBars?: number
  minDays?: number
}

/**
 * The smallest valid window: the largest lower bound that a published
 * constraint puts on the window field (``window >= rhs`` or ``window > rhs``),
 * evaluated for the current values. ``undefined`` when none can be evaluated.
 */
export function minimumWindow(
  constraints: ConstraintRule[],
  values: FieldValues,
  lookups: Lookups,
): number | undefined {
  let minimum: number | undefined
  for (const rule of constraints) {
    const lhs = rule.lhs
    if (!isRecord(lhs) || lhs.field !== HISTORY_BARS_FIELD) continue
    if (rule.cmp !== "gte" && rule.cmp !== "gt") continue
    const rhs = evaluateExpr(rule.rhs, values, lookups)
    if (rhs === undefined) continue
    const bound = Number(formatDecimal(rhs))
    if (!Number.isFinite(bound)) continue
    const bars = rule.cmp === "gt" ? Math.floor(bound) + 1 : Math.ceil(bound)
    minimum = minimum === undefined ? bars : Math.max(minimum, bars)
  }
  return minimum
}

/** Required history of every interval field: bars × interval (D4). */
export function requiredHistory(
  fields: StrategyField[],
  values: FieldValues,
  lookups: Lookups,
  constraints: ConstraintRule[] = [],
): HistoryHint[] {
  const barsText = values[HISTORY_BARS_FIELD]
  if (typeof barsText !== "string" || !INTEGER_TEXT.test(barsText)) return []
  const bars = Number(barsText)
  if (!Number.isSafeInteger(bars) || bars <= 0) return []
  const table = lookups.intervals_ms ?? {}
  const minBars = minimumWindow(constraints, values, lookups)
  const hints: HistoryHint[] = []
  for (const field of fields) {
    if (field.widget !== "interval") continue
    const interval = values[field.key]
    if (typeof interval !== "string" || !Object.hasOwn(table, interval))
      continue
    const ms = bars * table[interval]
    hints.push({
      field: field.key,
      interval,
      days: ms / 86_400_000,
      tooLong: ms > HISTORY_WARNING_MS,
      minBars,
      minDays:
        minBars === undefined
          ? undefined
          : (minBars * table[interval]) / 86_400_000,
    })
  }
  return hints
}

// --- Option list editor (§4.4) ---------------------------------------------------

/**
 * One entry of an option list as the editor stores it, the backend's rule:
 * trimmed; for an interval field an exact code of S is kept as is (case
 * matters: `1m` is a minute, `1M` a month), a strict label becomes its code
 * (`H1` → `1h`, `W1` → `1w`), anything else is kept as typed and shown as
 * unsupported (D3). Other fields are only trimmed.
 */
export function normalizeOption(
  field: StrategyField | undefined,
  raw: string,
): string {
  const value = raw.trim()
  if (field?.widget !== "interval") return value
  if ((field.options ?? []).includes(value)) return value
  const match = /^([MHDW])([0-9]+)$/.exec(value)
  if (!match) return value
  return `${match[2].replace(/^0+/, "") || "0"}${match[1].toLowerCase()}`
}

/** Why an entry cannot join a list, or `null`: empty, or a duplicate after
 * normalization (`H1` and `1h` are one value). */
export function optionEntryProblem(
  value: string,
  list: string[],
): string | null {
  if (value === "") return "Enter a value."
  if (list.includes(value)) return `${value} is already in the list.`
  return null
}
