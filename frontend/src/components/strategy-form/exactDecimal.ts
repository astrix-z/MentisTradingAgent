// Exact decimal numbers for the settings form (plan §4.5.5): a `BigInt`
// mantissa and a power-of-ten exponent, so bounds, cross-field rules and the
// "modified" comparison never pass through a float. Only what the published
// constraint grammar needs is here: add, sub, mul, max and comparison.

/** `m × 10^e`, kept with no trailing zeros in `m` (zero is `0 × 10^0`). */
export type Decimal = { m: bigint; e: number }

/** Integer fields: an optional sign and digits, nothing else. */
export const INTEGER_TEXT = /^[+-]?\d+$/
/** Decimal fields: the backend grammar; no spaces, no `0x`, no `Infinity`. */
export const DECIMAL_TEXT = /^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$/

/** Digits and exponent of a literal, before it becomes a `Decimal`. */
type Literal = { negative: boolean; digits: string; exponent: number }

/** Bounds of the numeric domain shared with the backend (§4.3.5 step 1). */
const MAX_SIGNIFICANT = 64
const MAX_ADJUSTED = 64

function literal(text: string): Literal | undefined {
  const match = DECIMAL_TEXT.exec(text)
  if (!match) return undefined
  const negative = text.startsWith("-")
  const body = text.replace(/^[+-]/, "")
  const [mantissa, exponentText] = body.split(/[eE]/)
  const [whole, fraction = ""] = mantissa.split(".")
  const digits = `${whole}${fraction}`.replace(/^0+/, "")
  // An exponent too long for a double is outside the domain either way; the
  // value only matters when the digits are all zero.
  const exponent = Number(exponentText ?? "0") - fraction.length
  return { negative, digits, exponent }
}

/**
 * Whether a literal is inside the numeric domain: zero in any spelling, or at
 * most 64 significant digits (trailing zeros do not count) with an adjusted
 * exponent in [-64, 64]. Judged by value, never by rounding (R63).
 */
function inDomain({ digits, exponent }: Literal): boolean {
  if (digits === "") return true
  const significant = digits.replace(/0+$/, "").length
  const adjusted = exponent + digits.length - 1
  return (
    significant <= MAX_SIGNIFICANT &&
    Number.isFinite(adjusted) &&
    adjusted >= -MAX_ADJUSTED &&
    adjusted <= MAX_ADJUSTED
  )
}

function normalized(m: bigint, e: number): Decimal {
  if (m === 0n) return { m: 0n, e: 0 }
  let mantissa = m
  let exponent = e
  while (mantissa % 10n === 0n) {
    mantissa /= 10n
    exponent += 1
  }
  return { m: mantissa, e: exponent }
}

export type ParsedDecimal =
  | { kind: "ok"; value: Decimal }
  | { kind: "format" }
  | { kind: "too_large" }

/** Parse decimal text: grammar first, then the domain, then the value. */
export function parseDecimal(text: string): ParsedDecimal {
  const parts = literal(text)
  if (!parts) return { kind: "format" }
  if (!inDomain(parts)) return { kind: "too_large" }
  if (parts.digits === "") return { kind: "ok", value: { m: 0n, e: 0 } }
  const m = BigInt(parts.digits) * (parts.negative ? -1n : 1n)
  return { kind: "ok", value: normalized(m, parts.exponent) }
}

/** A JSON number (a bound, a lookup, a constant) as an exact decimal. */
export function decimalFromNumber(value: number): Decimal | undefined {
  if (!Number.isFinite(value)) return undefined
  const parsed = parseDecimal(String(value))
  return parsed.kind === "ok" ? parsed.value : undefined
}

function aligned(a: Decimal, b: Decimal): [bigint, bigint, number] {
  const e = Math.min(a.e, b.e)
  return [a.m * 10n ** BigInt(a.e - e), b.m * 10n ** BigInt(b.e - e), e]
}

export function compare(a: Decimal, b: Decimal): -1 | 0 | 1 {
  const [x, y] = aligned(a, b)
  return x < y ? -1 : x > y ? 1 : 0
}

export function add(a: Decimal, b: Decimal): Decimal {
  const [x, y, e] = aligned(a, b)
  return normalized(x + y, e)
}

export function sub(a: Decimal, b: Decimal): Decimal {
  const [x, y, e] = aligned(a, b)
  return normalized(x - y, e)
}

export function mul(a: Decimal, b: Decimal): Decimal {
  return normalized(a.m * b.m, a.e + b.e)
}

export function max(a: Decimal, b: Decimal): Decimal {
  return compare(a, b) >= 0 ? a : b
}

/** Plain positional notation: `1000`, `0.25`, `-3.5`. */
export function formatDecimal({ m, e }: Decimal): string {
  const negative = m < 0n
  const digits = (negative ? -m : m).toString()
  let text: string
  if (e >= 0) text = digits + "0".repeat(e)
  else {
    const padded = digits.padStart(-e + 1, "0")
    text = `${padded.slice(0, e)}.${padded.slice(e)}`
  }
  return negative ? `-${text}` : text
}
