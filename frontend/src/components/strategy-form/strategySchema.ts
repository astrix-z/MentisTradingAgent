// Renders and validates strategy settings straight from the JSON Schema and
// constraint AST the agent publishes (`StrategyDescription.settings_schema`
// and `.constraints`). Nothing here names a specific strategy: labels come
// from each property's `title`, values from its `default`, and cross-field
// rules are interpreted from the declarative constraint expressions.

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
  /** Default serialized for a text input (arrays join with ", "). */
  defaultText: string
  /** Allowed values for `enum` fields. */
  options?: string[]
  /** Numeric bounds for scalar and array-item fields. */
  bounds?: StrategyBounds
  minItems?: number
  maxItems?: number
  /** Element type for `array` fields. */
  itemKind?: "integer" | "number"
}

export type FieldValues = Record<string, string>
export type FieldErrors = Record<string, string>
export type Lookups = Record<string, Record<string, number>>
/** A constraint rule exactly as published; keys are validated at runtime. */
export type ConstraintRule = Record<string, unknown>

export type ValidationResult =
  | { ok: true; params: Record<string, unknown> }
  | { ok: false; fieldErrors: FieldErrors; formErrors: string[] }

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

/** Serialize a param value the way a text input shows it. */
export function formatParamValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => String(item)).join(", ")
  if (value === undefined || value === null) return ""
  return String(value)
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

    // Fixed value: the schema pins it with `const`.
    if (prop.const !== undefined) {
      fields.push({
        key,
        title,
        kind: "const",
        defaultText: formatParamValue(prop.const),
      })
      continue
    }

    // Dropdown of allowed strings (includes the interval widget).
    if (Array.isArray(prop.enum)) {
      const options = prop.enum.map((option) => String(option))
      fields.push({
        key,
        title,
        kind: "enum",
        options,
        defaultText:
          prop.default !== undefined
            ? String(prop.default)
            : (options[0] ?? ""),
      })
      continue
    }

    // Fixed-length numeric list.
    if (prop.type === "array") {
      const prefixItems = Array.isArray(prop.prefixItems)
        ? prop.prefixItems
        : []
      const item = isRecord(prefixItems[0]) ? prefixItems[0] : undefined
      const count = prefixItems.length
      fields.push({
        key,
        title,
        kind: "array",
        defaultText: formatParamValue(prop.default),
        minItems: readNumber(prop.minItems) ?? count,
        maxItems: readNumber(prop.maxItems) ?? count,
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
          defaultText: formatParamValue(prop.default),
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
        defaultText: formatParamValue(prop.default),
        bounds: boundsFrom(prop),
      })
      continue
    }

    if (prop.type === "number") {
      fields.push({
        key,
        title,
        kind: "decimal",
        defaultText: formatParamValue(prop.default),
        bounds: boundsFrom(prop),
      })
      continue
    }

    fields.push({
      key,
      title,
      kind: "text",
      defaultText: formatParamValue(prop.default),
    })
  }
  return fields
}

/** Initial draft: every field at the default the schema declares. */
export function initialFormValues(fields: StrategyField[]): FieldValues {
  const values: FieldValues = {}
  for (const field of fields) values[field.key] = field.defaultText
  return values
}

/**
 * Overlay the agent's saved desired params on top of the schema defaults.
 * `legacyKeys` (old name -> new name, from the catalog) reads a value saved
 * under a renamed parameter's old name; the new name wins if both are saved.
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
    values[field.key] = formatParamValue(raw)
  }
  return values
}

function boundErrorFor(
  title: string,
  value: number,
  bounds: StrategyBounds,
): string | undefined {
  if (bounds.gt !== undefined && !(value > bounds.gt))
    return `${title} must be greater than ${bounds.gt}`
  if (bounds.gte !== undefined && !(value >= bounds.gte))
    return `${title} must be greater than or equal to ${bounds.gte}`
  if (bounds.lt !== undefined && !(value < bounds.lt))
    return `${title} must be less than ${bounds.lt}`
  if (bounds.lte !== undefined && !(value <= bounds.lte))
    return `${title} must be less than or equal to ${bounds.lte}`
  return undefined
}

function validateFieldValue(
  field: StrategyField,
  raw: string,
): { value?: unknown; error?: string } {
  const trimmed = raw.trim()
  if (field.kind === "const") return { value: field.defaultText }
  if (trimmed === "") return { error: `${field.title} is required` }

  if (field.kind === "text") return { value: trimmed }

  if (field.kind === "enum") {
    const options = field.options ?? []
    if (!options.includes(trimmed))
      return {
        value: trimmed,
        error: `${field.title} must be one of: ${options.join(", ")}`,
      }
    return { value: trimmed }
  }

  if (field.kind === "array") {
    const parts = trimmed.split(",").map((part) => part.trim())
    const numbers: number[] = []
    for (const part of parts) {
      const parsed = Number(part)
      // Reject the whole list if any element is not a finite number.
      if (part === "" || !Number.isFinite(parsed))
        return {
          error: `${field.title} must be a comma-separated list of numbers`,
        }
      numbers.push(parsed)
    }
    const min = field.minItems ?? numbers.length
    const max = field.maxItems ?? numbers.length
    if (numbers.length < min || numbers.length > max) {
      const expected =
        min === max ? `exactly ${min}` : `between ${min} and ${max}`
      return { error: `${field.title} must have ${expected} values` }
    }
    if (
      field.itemKind === "integer" &&
      numbers.some((n) => !Number.isInteger(n))
    )
      return {
        error: `${field.title} must be a comma-separated list of whole numbers`,
      }
    for (const n of numbers) {
      const boundError = boundErrorFor(field.title, n, field.bounds ?? {})
      if (boundError) return { error: boundError }
    }
    return { value: numbers }
  }

  const parsed = Number(trimmed)
  if (!Number.isFinite(parsed))
    return { value: trimmed, error: `${field.title} must be a finite number` }

  const boundError = boundErrorFor(field.title, parsed, field.bounds ?? {})
  if (boundError) return { value: trimmed, error: boundError }

  if (field.kind === "integer") {
    if (!Number.isInteger(parsed))
      return { value: trimmed, error: `${field.title} must be a whole number` }
    return { value: parsed }
  }

  // Decimals stay strings so precision is preserved on the wire.
  return { value: trimmed }
}

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

function toNumber(value: unknown): number | undefined {
  if (typeof value === "number")
    return Number.isFinite(value) ? value : undefined
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : undefined
  }
  return undefined
}

/** Evaluate one constraint expression to a number, or `undefined` if the
 * payload cannot be evaluated (unknown operator, missing lookup key, ...). */
function evaluateExpr(
  expr: unknown,
  values: Record<string, unknown>,
  lookups: Lookups,
): number | undefined {
  if (!isRecord(expr)) return undefined

  if ("const" in expr) return readNumber(expr.const)

  if ("lookup" in expr) {
    const tableName = expr.lookup
    if (typeof tableName !== "string") return undefined
    const raw = resolveField(expr.field, values)
    if (raw === undefined) return undefined
    const table = lookups[tableName]
    if (!table) return undefined
    return toNumber(table[String(raw)])
  }

  if ("field" in expr) return toNumber(resolveField(expr.field, values))

  if ("op" in expr) {
    const args = expr.args
    if (!Array.isArray(args)) return undefined
    const numbers: number[] = []
    for (const arg of args) {
      const value = evaluateExpr(arg, values, lookups)
      if (value === undefined) return undefined
      numbers.push(value)
    }
    if (numbers.length === 0) return undefined
    switch (expr.op) {
      case "add":
        return numbers.reduce((sum, value) => sum + value, 0)
      case "mul":
        return numbers.reduce((product, value) => product * value, 1)
      case "max":
        return Math.max(...numbers)
      case "sub":
        return numbers.slice(1).reduce((acc, value) => acc - value, numbers[0])
      default:
        return undefined
    }
  }

  return undefined
}

function compareExpr(
  cmp: string,
  lhs: number,
  rhs: number,
): boolean | undefined {
  switch (cmp) {
    case "lt":
      return lhs < rhs
    case "lte":
      return lhs <= rhs
    case "gt":
      return lhs > rhs
    case "gte":
      return lhs >= rhs
    case "eq":
      return lhs === rhs
    case "ne":
      return lhs !== rhs
    default:
      return undefined
  }
}

/** Integer values render without a trailing `.0`. */
function formatNumber(value: number): string {
  return String(value)
}

function substituteMessage(message: string, lhs: number, rhs: number): string {
  return message
    .replace(/\{lhs\}/g, formatNumber(lhs))
    .replace(/\{rhs\}/g, formatNumber(rhs))
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
    if (typeof cmp !== "string" || typeof message !== "string")
      return { messages: [...messages], evaluable: false }
    const lhs = evaluateExpr(rule.lhs, values, lookups)
    const rhs = evaluateExpr(rule.rhs, values, lookups)
    if (lhs === undefined || rhs === undefined)
      return { messages: [...messages], evaluable: false }
    const passed = compareExpr(cmp, lhs, rhs)
    if (passed === undefined)
      return { messages: [...messages], evaluable: false }
    if (!passed) messages.add(substituteMessage(message, lhs, rhs))
  }
  return { messages: [...messages], evaluable: true }
}

/**
 * Validate the whole draft against the field schemas and the published
 * constraints. Params are exactly the schema's fields (const ones included):
 * a key the schema does not name — an old parameter name or anything else
 * left in the saved desired params — is never sent, since the backend
 * rejects it.
 */
export function validateSettings(options: {
  fields: StrategyField[]
  values: FieldValues
  lookups: Lookups
  constraints: ConstraintRule[]
}): ValidationResult {
  const { fields, values, lookups, constraints } = options
  const fieldErrors: FieldErrors = {}
  const parsed: Record<string, unknown> = {}
  for (const field of fields) {
    const { value, error } = validateFieldValue(field, values[field.key] ?? "")
    if (error) fieldErrors[field.key] = error
    else parsed[field.key] = value
  }
  if (Object.keys(fieldErrors).length > 0)
    return { ok: false, fieldErrors, formErrors: [] }

  const constraintResult = runConstraints(constraints, parsed, lookups)
  if (!constraintResult.evaluable)
    return { ok: false, fieldErrors: {}, formErrors: [UNABLE_TO_EVALUATE] }
  if (constraintResult.messages.length > 0)
    return { ok: false, fieldErrors: {}, formErrors: constraintResult.messages }

  return { ok: true, params: parsed }
}
