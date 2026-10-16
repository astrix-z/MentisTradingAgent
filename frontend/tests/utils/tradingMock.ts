// The API a trading page talks to, mocked for Playwright: status, catalog,
// working symbols, commands, presets and option lists (shared by the
// contract spec and the runner of the validation fixture).
import { expect, type Page } from "@playwright/test"
import type {
  CommandEnvelope,
  CommandResult,
  FieldOptionsOut,
  PositionsList,
  PresetList,
  PresetOut,
  StrategyCatalog,
  SymbolEntry,
  TradingStatus,
  WorkingSymbols,
} from "../../src/client"
import strategyCatalog from "../fixtures/strategy-catalog.json" with {
  type: "json",
}

export const firstId = "11111111-1111-4111-8111-111111111111"
export const secondId = "22222222-2222-4222-8222-222222222222"
export const at = "2026-09-12T12:00:00Z"

// The agent publishes this document (see fixtures/strategy-catalog.json); the
// backend serves it wrapped in a StrategyCatalog with `available: true`.
export function catalog(body: unknown = strategyCatalog): StrategyCatalog {
  return { available: true, ...(body as object) }
}
export function snapshot(): TradingStatus {
  return {
    desired: {
      running: true,
      network: "live",
      strategy: "tripple_screen",
      symbols: ["ETHUSDT"],
      // Saved before the rename (old names) and by an older agent (future_param).
      params: {
        tp_pct: 17,
        ema_tide_periods: [6, 14, 35],
        swing_low_h1_n: 7,
        trail_m15_n: 9,
        future_param: 42,
      },
      updated_at: at,
      revision: 3,
    },
    actual: {
      state: "running",
      network: "live",
      strategy: "tripple_screen",
      balances: { USDT: 123 },
      updated_at: at,
    },
    online: true,
  }
}
// The agent publishes the tradeable universe with the marks merged in; the
// backend serves it verbatim. ETHUSDT is marked so a Start still carries it.
export function workingSymbols(
  overrides: Partial<WorkingSymbols> = {},
): WorkingSymbols {
  const entry = (
    symbol: string,
    baseAsset: string,
    extra: Partial<SymbolEntry> = {},
  ): SymbolEntry => ({
    symbol,
    base_asset: baseAsset,
    quote_asset: "USDT",
    status: "TRADING",
    spot_allowed: true,
    quote_volume_24h: "1000",
    classification: "crypto",
    classification_label: null,
    excluded_reason: null,
    monitoring: false,
    selected: false,
    ...extra,
  })
  return {
    available: true,
    quote_asset: "USDT",
    generated_at: at,
    prices_updated_at: "2026-09-15T11:30:00Z",
    monitoring_updated_at: "2026-09-15T09:05:00Z",
    total: 3,
    usable: 2,
    selected: 1,
    selected_unusable: 0,
    symbols: [
      entry("AAPLBUSDT", "AAPLB", {
        classification: "tokenized_equity",
        classification_label: "tokenized equity",
        excluded_reason: "tokenized equity",
        last_price: "180.25",
      }),
      // Deliberately huge: 2500000000 must sort after 900, which only happens
      // if the Decimal strings are compared as numbers.
      entry("BTCUSDT", "BTC", {
        quote_volume_24h: "2500000000",
        last_price: "64000.10",
      }),
      // No price, and under Binance's Monitoring tag: the column has to show "no
      // reading" rather than zero, and the tag is a label rather than a verdict.
      entry("ETHUSDT", "ETH", {
        quote_volume_24h: "900",
        selected: true,
        monitoring: true,
      }),
    ],
    ...overrides,
  }
}

//: A universe long enough to need pagination, for the rows-per-page control.
export function manySymbols(count: number): WorkingSymbols {
  const symbols: SymbolEntry[] = []
  for (let index = 0; index < count; index += 1) {
    const name = `SYM${index.toString().padStart(3, "0")}`
    symbols.push({
      symbol: `${name}USDT`,
      base_asset: name,
      quote_asset: "USDT",
      status: "TRADING",
      spot_allowed: true,
      quote_volume_24h: `${count - index}`,
      classification: "crypto",
      classification_label: null,
      excluded_reason: null,
      selected: index < 3,
    })
  }
  return {
    ...workingSymbols(),
    total: count,
    usable: count,
    selected: 3,
    symbols,
  }
}

// A just-issued command, before its result arrives. The tracker polls every
// 1.5 s on a timer the test clock cannot hold back, so "accepted" can already
// read "pending" when a slow run asserts; both mean "accepted and tracked".
export function accepted(type: string): RegExp {
  return new RegExp(`^${type} — (accepted|pending)$`)
}

// The option lists the migration seeds (plan §4.3.1), newest timeframe first.
export function baselineOptions(): Record<string, string[]> {
  return {
    quote_asset: ["USDT", "USDC"],
    tide_interval: ["1w", "1d", "12h", "6h", "4h", "2h", "1h", "30m", "15m"],
    wave_interval: ["1d", "12h", "6h", "4h", "2h", "1h", "30m", "15m", "5m"],
    ripple_interval: ["12h", "6h", "4h", "2h", "1h", "30m", "15m", "5m", "1m"],
  }
}

export const builtinIds: Record<string, string> = {
  Default: "d0000000-0000-4000-8000-000000000001",
  Fast: "d0000000-0000-4000-8000-000000000002",
}

// The built-in presets the agent publishes, as `GET presets/` serves them.
export function builtinPresets(): PresetOut[] {
  return strategyCatalog.strategies[0].presets.map((preset) => ({
    id: builtinIds[preset.name],
    name: preset.name,
    is_builtin: true,
    params: structuredClone(preset.params) as Record<string, unknown>,
    valid: true,
    errors: [],
  }))
}

export type PresetApi = {
  list: PresetList
  options: Record<string, string[]>
  /** Anything but 200 fails the options read. */
  optionsStatus: number
  writes: { method: string; path: string; body: unknown }[]
}

/** Pick an option of a `Select` field by its visible label. */
export async function choose(page: Page, field: string, option: string) {
  await page.getByLabel(field, { exact: true }).click()
  await page.getByRole("option", { name: option, exact: true }).click()
}

/** The live check has run: Start is off until the draft is fixed. */
export async function expectBlocked(page: Page) {
  await expect(
    page.getByRole("button", { name: "Start", exact: true }),
  ).toBeDisabled()
}

export async function mock(
  page: Page,
  status = snapshot(),
  published: StrategyCatalog = catalog(),
  universe: WorkingSymbols = workingSymbols(),
) {
  const presets: PresetApi = {
    list: { items: builtinPresets(), builtin_sync: { status: "ok" } },
    options: baselineOptions(),
    optionsStatus: 200,
    writes: [],
  }
  const posts: { path: string; body: Record<string, unknown> | null }[] = []
  const results = new Map<string, CommandResult | null>()
  const reads: string[] = []
  await page.addInitScript(() =>
    localStorage.setItem("access_token", "mock-token"),
  )
  await page.route("**/api/v1/**", async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    let body: unknown
    let code = 200
    if (url.pathname.endsWith("/users/me")) {
      body = {
        id: firstId,
        email: "test@example.com",
        is_superuser: true,
        is_active: true,
      }
    } else if (url.pathname.endsWith("/trading/status/")) {
      body = status
    } else if (url.pathname.endsWith("/trading/result/")) {
      const id = url.searchParams.get("command_id") ?? "MISSING"
      reads.push(id)
      body = results.get(id) ?? null
    } else if (url.pathname.endsWith("/trading/strategies/")) {
      body = published
    } else if (url.pathname.includes("/trading/strategies/tripple_screen/")) {
      const tail = url.pathname.split("/tripple_screen/")[1]
      const method = request.method()
      if (tail === "options/") {
        const supported = new Set(
          strategyCatalog.strategies[0].settings_schema.properties
            .ripple_interval.enum,
        )
        const out: Record<string, FieldOptionsOut> = {}
        for (const [field, options] of Object.entries(presets.options))
          out[field] = {
            options,
            unsupported:
              field === "quote_asset"
                ? []
                : options.filter((option) => !supported.has(option)),
          }
        body = out
        code = presets.optionsStatus
      } else if (tail.startsWith("options/") && method === "PUT") {
        const field = tail.slice("options/".length)
        const payload = request.postDataJSON() as { options: string[] }
        presets.writes.push({ method, path: tail, body: payload })
        presets.options[field] = payload.options
        body = { options: payload.options, unsupported: [] }
      } else if (tail === "presets/" && method === "GET") {
        body = presets.list
      } else if (tail === "presets/" && method === "POST") {
        const payload = request.postDataJSON() as PresetOut
        presets.writes.push({ method, path: tail, body: payload })
        const created: PresetOut = {
          id: `c0000000-0000-4000-8000-00000000000${presets.writes.length}`,
          name: payload.name,
          is_builtin: false,
          params: payload.params,
          valid: true,
          errors: [],
        }
        presets.list.items.push(created)
        body = created
        code = 201
      } else if (tail.startsWith("presets/")) {
        const id = tail.slice("presets/".length)
        const index = presets.list.items.findIndex((item) => item.id === id)
        const payload = method === "PUT" ? request.postDataJSON() : null
        presets.writes.push({ method, path: tail, body: payload })
        if (method === "DELETE") {
          presets.list.items.splice(index, 1)
          await route.fulfill({ status: 204 })
          return
        }
        presets.list.items[index] = {
          ...presets.list.items[index],
          params: payload.params,
        }
        body = presets.list.items[index]
      } else {
        throw new Error(`Unexpected API request: ${method} ${url}`)
      }
    } else if (url.pathname.endsWith("/trading/positions/")) {
      // The dashboard at "/" reads positions; none have been recorded here.
      body = { positions: [] } satisfies PositionsList
    } else if (
      request.method() === "GET" &&
      url.pathname.endsWith("/trading/symbols/")
    ) {
      body = universe
    } else if (request.method() === "POST") {
      const payload = request.postDataJSON()
      posts.push({ path: url.pathname, body: payload })
      // The generic endpoint names its type in the body; the others in the path.
      const generic = url.pathname.endsWith("/trading/commands/")
      const configure = url.pathname.endsWith("/trading/execution/")
      const envelope: CommandEnvelope = {
        id: posts.length === 1 ? firstId : secondId,
        type: generic
          ? payload.type
          : configure
            ? "execution_configure"
            : (url.pathname.split("/").at(-2) ?? ""),
        payload: (generic ? payload.payload : payload) ?? {},
        issued_at: at,
      }
      body = envelope
      code = 202
    } else {
      throw new Error(`Unexpected API request: ${request.method()} ${url}`)
    }
    await route.fulfill({ status: code, json: body })
  })
  return { posts, results, reads, status, universe, presets }
}
