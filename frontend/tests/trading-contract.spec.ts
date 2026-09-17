import { expect, type Page, test } from "@playwright/test"
import type {
  CommandEnvelope,
  CommandResult,
  StrategyCatalog,
  SymbolEntry,
  TradingStatus,
  WorkingSymbols,
} from "../src/client"
import strategyCatalog from "./fixtures/strategy-catalog.json"

const firstId = "11111111-1111-4111-8111-111111111111"
const secondId = "22222222-2222-4222-8222-222222222222"
const at = "2026-09-12T12:00:00Z"

// The agent publishes this document (see fixtures/strategy-catalog.json); the
// backend serves it wrapped in a StrategyCatalog with `available: true`.
function catalog(body: unknown = strategyCatalog): StrategyCatalog {
  return { available: true, ...(body as object) }
}
function snapshot(): TradingStatus {
  return {
    desired: {
      running: true,
      network: "live",
      strategy: "tripple_screen",
      symbols: ["ETHUSDT"],
      params: { tp_pct: 17, ema_tide_periods: [6, 14, 35], future_param: 42 },
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
function workingSymbols(
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
function manySymbols(count: number): WorkingSymbols {
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

async function mock(
  page: Page,
  status = snapshot(),
  published: StrategyCatalog = catalog(),
  universe: WorkingSymbols = workingSymbols(),
) {
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
    } else if (
      request.method() === "GET" &&
      url.pathname.endsWith("/trading/symbols/")
    ) {
      body = universe
    } else if (request.method() === "POST") {
      const payload = request.postDataJSON()
      posts.push({ path: url.pathname, body: payload })
      const envelope: CommandEnvelope = {
        id: posts.length === 1 ? firstId : secondId,
        type: url.pathname.split("/").at(-2) ?? "",
        payload: payload ?? {},
        issued_at: at,
      }
      body = envelope
      code = 202
    } else {
      throw new Error(`Unexpected API request: ${request.method()} ${url}`)
    }
    await route.fulfill({ status: code, json: body })
  })
  return { posts, results, reads, status, universe }
}

test("legacy /trading route redirects to /balance", async ({ page }) => {
  await mock(page)
  await page.goto("/trading")
  await expect(page).toHaveURL(/\/balance$/)
})

test("sidebar links to /balance, not /trading", async ({ page }) => {
  await mock(page)
  await page.goto("/")
  await expect(
    page.getByRole("link", { name: "Balance", exact: true }),
  ).toHaveAttribute("href", "/balance")
  await expect(page.getByRole("link", { name: "Trading" })).toHaveCount(0)
})

test("sidebar links to /working-symbols from Strategy", async ({ page }) => {
  await mock(page)
  await page.goto("/")
  await expect(
    page.getByRole("link", { name: "Working Symbols", exact: true }),
  ).toHaveAttribute("href", "/working-symbols")
})

test("balance page is observation-only and separates desired, actual and balance networks", async ({
  page,
}) => {
  const state = snapshot()
  state.desired.network = "testnet"
  state.online = false
  const api = await mock(page, state)
  await page.goto("/balance")
  await expect(
    page.getByText("Actual network: LIVE", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("Desired network: TESTNET", { exact: true }),
  ).toBeVisible()
  await expect(
    page.getByText("Heartbeat balances — LIVE (stale)"),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Start", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "Stop", exact: true }),
  ).toHaveCount(0)
  await expect(
    page.getByRole("button", { name: "Check balance", exact: true }),
  ).toBeDisabled()
  await page.getByRole("button", { name: "TESTNET", exact: true }).click()
  await page.getByRole("button", { name: "TESTNET", exact: true }).click()
  await page.getByRole("button", { name: "LIVE", exact: true }).click()
  expect(api.posts).toEqual([])
  await page.getByRole("button", { name: "Check balance", exact: true }).click()
  await expect(page.getByText("balance — accepted")).toBeVisible()
  expect(api.posts[0].body).toEqual({ network: "live" })
})

test("strategy seeds desired config, preserves dirty draft and reports rejected start", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/strategy")
  // Symbols are not typed here any more: they come from the working list,
  // which the page links to instead of offering its own input.
  await expect(
    page.getByRole("link", { name: "Working Symbols", exact: true }).first(),
  ).toHaveAttribute("href", "/working-symbols")
  await expect(
    page.getByText(/1 symbol\(s\) from the working list/),
  ).toBeVisible()
  await expect(page.getByLabel("Symbols", { exact: true })).toHaveCount(0)
  await expect(page.getByLabel("Take profit %")).toHaveValue("17")
  await expect(page.getByLabel("EMA tide periods")).toHaveValue("6, 14, 35")
  await expect(
    page.getByRole("button", { name: "LIVE", exact: true }),
  ).toHaveAttribute("aria-pressed", "true")
  await page.getByLabel("Take profit %").fill("19")
  api.status.desired.params = { tp_pct: 25 }
  await page.clock.install()
  await page.clock.runFor(5500)
  await expect(page.getByLabel("Take profit %")).toHaveValue("19")
  await expect(page.getByText(/Unsaved local draft/)).toBeVisible()
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "Confirm live Start" }),
  ).toBeVisible()
  await page
    .getByRole("button", { name: "Confirm live Start", exact: true })
    .click()
  await expect(page.getByText("start — accepted")).toBeVisible()
  // Decimals are sent as strings so precision survives the round-trip.
  expect(api.posts[0].body).toMatchObject({
    network: "live",
    symbols: ["ETHUSDT"],
    params: { tp_pct: "19", future_param: 42 },
  })
  api.results.set(firstId, {
    command_id: firstId,
    type: "start",
    ok: false,
    message: "Invalid start settings",
    at,
  })
  await page.clock.runFor(1600)
  await expect(page.getByText("start — failed")).toBeVisible()
  await expect(page.getByText("Invalid start settings")).toBeVisible()
  await page.reload()
  await expect(page.getByLabel("Take profit %")).toHaveValue("25")
})

test("a monitored symbol is labelled, never filtered", async ({ page }) => {
  const api = await mock(page)
  await page.goto("/working-symbols")

  const rowOf = (symbol: string) =>
    page.locator("tbody tr", { hasText: symbol })

  // The badge lives in the Marks column, and says what it means.
  await expect(
    rowOf("ETHUSDT").getByText("Monitoring", { exact: true }),
  ).toBeVisible()
  await expect(rowOf("BTCUSDT").getByText("Monitoring")).toHaveCount(0)
  await expect(page.getByText(/Monitoring list read/)).toBeVisible()

  // A label, not a verdict: the monitored symbol is still usable, still
  // markable, and still counted among the usable ones.
  await expect(
    page.getByText("Marked: 1 · Usable: 2 · Listed: 3 · Monitoring: 1"),
  ).toBeVisible()
  const monitored = page.getByRole("checkbox", {
    name: "Use ETHUSDT in the strategy",
  })
  await expect(monitored).toBeEnabled()
  await expect(monitored).toBeChecked()

  // And marking it still saves like any other symbol.
  await page.clock.install()
  await monitored.click()
  await page.getByRole("button", { name: "Save", exact: true }).click()
  await expect(page.getByText("symbols — accepted")).toBeVisible()
  expect(api.posts[0].path).toContain("/trading/symbols/")
  expect(api.posts[0].body).toEqual({ symbols: [] })
})

test("working symbols: filter, sort on every column and save the whole set", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/working-symbols")

  // Only usable symbols are listed until the toggle asks for the rest.
  await expect(page.getByText("AAPLBUSDT")).toHaveCount(0)
  await expect(page.getByText("BTCUSDT")).toBeVisible()
  await expect(page.getByText("ETHUSDT")).toBeVisible()
  await expect(
    page.getByRole("checkbox", { name: "Use ETHUSDT in the strategy" }),
  ).toBeChecked()

  // Every column sorts; the volume column compares numerically, so 900 comes
  // before 2500000000, which text comparison would get backwards.
  await page.getByRole("button", { name: "24h volume (USDT)" }).click()
  await expect(page.locator("tbody tr td:nth-child(2)")).toHaveText([
    "ETHUSDT",
    "BTCUSDT",
  ])
  // Clicking the same header again reverses the order.
  await page.getByRole("button", { name: "24h volume (USDT)" }).click()
  await expect(page.locator("tbody tr td:nth-child(2)")).toHaveText([
    "BTCUSDT",
    "ETHUSDT",
  ])

  // An excluded symbol can be inspected but not marked.
  await page.getByRole("switch", { name: "Show unusable symbols" }).click()
  await expect(page.getByText("AAPLBUSDT")).toBeVisible()
  await expect(
    page.getByRole("checkbox", { name: "Use AAPLBUSDT in the strategy" }),
  ).toBeDisabled()

  // Marking is a local draft until saved, and a save sends the whole set.
  await page.clock.install()
  await page
    .getByRole("checkbox", { name: "Use BTCUSDT in the strategy" })
    .click()
  await expect(page.getByText("Unsaved changes")).toBeVisible()
  expect(api.posts).toEqual([])
  await page.getByRole("button", { name: "Save", exact: true }).click()
  await expect(page.getByText("symbols — accepted")).toBeVisible()
  expect(api.posts[0].path).toContain("/trading/symbols/")
  expect(api.posts[0].body).toEqual({ symbols: ["BTCUSDT", "ETHUSDT"] })

  // The agent is the source of truth: once the save is confirmed the draft is
  // gone, and what the table shows is the document the agent republished.
  for (const entry of api.universe.symbols ?? []) {
    entry.selected = entry.symbol !== "AAPLBUSDT"
  }
  api.universe.selected = 2
  api.results.set(firstId, {
    command_id: firstId,
    type: "symbols",
    ok: true,
    data: { symbols: ["BTCUSDT", "ETHUSDT"], count: 2 },
    at,
  })
  await page.clock.runFor(2000)
  await expect(page.getByText("symbols — succeeded")).toBeVisible()
  await expect(page.getByText("Unsaved changes")).toHaveCount(0)
})

test("rows per page offers 100 and All, and All keeps the control reachable", async ({
  page,
}) => {
  await mock(page, snapshot(), catalog(), manySymbols(120))
  await page.goto("/working-symbols")

  // The default page size shows one page of ten.
  await expect(page.locator("tbody tr")).toHaveCount(10)
  await expect(page.getByText("Showing 1 to 10 of 120 entries")).toBeVisible()

  const rowsPerPage = page.getByRole("combobox")
  await rowsPerPage.click()
  await page.getByRole("option", { name: "100", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(100)
  await expect(page.getByText("Showing 1 to 100 of 120 entries")).toBeVisible()

  // "All" renders every row and still counts one page, so the footer — and so
  // the control needed to page again — must stay.
  await rowsPerPage.click()
  await page.getByRole("option", { name: "All", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(120)
  await expect(page.getByText("Showing 1 to 120 of 120 entries")).toBeVisible()
  await expect(rowsPerPage).toBeVisible()

  // And a smaller size brings pagination back, starting from the first page.
  await rowsPerPage.click()
  await page.getByRole("option", { name: "5", exact: true }).click()
  await expect(page.locator("tbody tr")).toHaveCount(5)
  await expect(page.getByText("Showing 1 to 5 of 120 entries")).toBeVisible()
})

test("bulk selection edits the draft once and never picks unusable symbols", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/working-symbols")

  const marked = (count: number) =>
    page.getByText(new RegExp(`Marked: ${count}(?!\\d)`))
  const checkbox = (symbol: string) =>
    page.getByRole("checkbox", { name: `Use ${symbol} in the strategy` })
  const threshold = page.getByLabel("Volume above (24h, USDT)")

  // Start from an empty draft, so what the bulk actions add is unambiguous.
  await checkbox("ETHUSDT").click()
  await expect(marked(0)).toBeVisible()

  // 950 selects BTCUSDT (2.5e9) and not ETHUSDT (900). AAPLBUSDT's 1000 clears
  // the bar too, but it is a tokenized equity, so it must stay out.
  await page.getByRole("switch", { name: "Show unusable symbols" }).click()
  await threshold.fill("950")
  await page.getByRole("button", { name: "Select above" }).click()
  await expect(marked(1)).toBeVisible()
  await expect(checkbox("BTCUSDT")).toBeChecked()
  await expect(checkbox("ETHUSDT")).not.toBeChecked()
  await expect(checkbox("AAPLBUSDT")).not.toBeChecked()

  // It adds to the draft rather than replacing it, and tolerates a grouped
  // value copied out of the volume column.
  await threshold.fill("500")
  await page.getByRole("button", { name: "Select above" }).click()
  await expect(marked(2)).toBeVisible()
  await threshold.fill("1,000")
  await expect(page.getByRole("button", { name: "Select above" })).toBeEnabled()

  // A threshold that is not a number cannot be applied at all.
  await threshold.fill("not a number")
  await expect(
    page.getByRole("button", { name: "Select above" }),
  ).toBeDisabled()

  // Select all covers every usable symbol, and still nothing else.
  await checkbox("ETHUSDT").click()
  await expect(marked(1)).toBeVisible()
  await page.getByRole("button", { name: "Select all" }).click()
  await expect(marked(2)).toBeVisible()
  await expect(checkbox("BTCUSDT")).toBeChecked()
  await expect(checkbox("ETHUSDT")).toBeChecked()
  await expect(checkbox("AAPLBUSDT")).not.toBeChecked()

  // Nothing reached the server: both actions edited the draft only, and no rule
  // was stored that could be reapplied on reload or when the strategy starts.
  expect(api.posts).toEqual([])
})

test("Monitoring selection and inversion can be saved as the whole set", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/working-symbols")

  const checkbox = (symbol: string) =>
    page.getByRole("checkbox", { name: `Use ${symbol} in the strategy` })

  // Start empty, then select by the exchange label. Only ETH carries Monitoring.
  await checkbox("ETHUSDT").click()
  await page.getByRole("button", { name: "Select Monitoring" }).click()
  await expect(checkbox("ETHUSDT")).toBeChecked()
  await expect(checkbox("BTCUSDT")).not.toBeChecked()

  // Inversion applies to usable symbols: ETH leaves the set and BTC enters it.
  await page.getByRole("button", { name: "Invert selection" }).click()
  await expect(checkbox("ETHUSDT")).not.toBeChecked()
  await expect(checkbox("BTCUSDT")).toBeChecked()

  // An unusable symbol stays outside every bulk operation.
  await page.getByRole("switch", { name: "Show unusable symbols" }).click()
  await expect(checkbox("AAPLBUSDT")).not.toBeChecked()
  await expect(checkbox("AAPLBUSDT")).toBeDisabled()
  expect(api.posts).toEqual([])

  // The actions only changed the normal draft; Save still sends its complete set.
  await page.getByRole("button", { name: "Save", exact: true }).click()
  await expect(page.getByText("symbols — accepted")).toBeVisible()
  expect(api.posts[0].path).toContain("/trading/symbols/")
  expect(api.posts[0].body).toEqual({ symbols: ["BTCUSDT"] })
})

test("prices are read on request and land in the price column", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/working-symbols")

  const priceCell = (symbol: string) =>
    page.locator("tbody tr", { hasText: symbol }).locator("td").nth(2)

  // What the agent last published is shown as it stands, and a symbol it priced
  // nothing for says so instead of showing zero.
  await expect(priceCell("BTCUSDT")).toHaveText("64,000.1")
  await expect(priceCell("ETHUSDT")).toHaveText("—")
  await expect(page.getByText(/Prices read at/)).toBeVisible()

  await page.clock.install()
  await page.getByRole("button", { name: "Request prices" }).click()
  await expect(page.getByText("prices — accepted")).toBeVisible()
  expect(api.posts[0].path).toContain("/trading/prices/")
  expect(api.posts[0].body).toBeFalsy()

  // The agent republishes the document, so the fresh numbers arrive through the
  // read the table already does rather than a second shape of the data.
  for (const entry of api.universe.symbols ?? []) {
    if (entry.symbol === "BTCUSDT") entry.last_price = "70000"
    if (entry.symbol === "ETHUSDT") entry.last_price = "3500.25"
  }
  api.universe.prices_updated_at = "2026-09-15T12:34:56Z"
  api.results.set(firstId, {
    command_id: firstId,
    type: "prices",
    ok: true,
    data: { prices: 3, total: 3, at: "2026-09-15T12:34:56Z" },
    at,
  })
  await page.clock.runFor(2000)
  await expect(page.getByText("prices — succeeded")).toBeVisible()
  await expect(priceCell("BTCUSDT")).toHaveText("70,000")
  await expect(priceCell("ETHUSDT")).toHaveText("3,500.25")
  // Reading prices is not an edit: an unsaved draft stays the user's.
  await expect(page.getByText("Unsaved changes")).toHaveCount(0)
})

test("a price refresh keeps the reader on the same page", async ({ page }) => {
  const api = await mock(page, snapshot(), catalog(), manySymbols(120))
  await page.goto("/working-symbols")
  await expect(page.getByText("Showing 1 to 10 of 120 entries")).toBeVisible()
  await page.getByRole("button", { name: "Go to next page" }).click()
  await expect(page.getByText("Showing 11 to 20 of 120 entries")).toBeVisible()

  await page.clock.install()
  await page.getByRole("button", { name: "Request prices" }).click()
  await expect(page.getByText("prices — accepted")).toBeVisible()
  api.results.set(firstId, {
    command_id: firstId,
    type: "prices",
    ok: true,
    at,
  })
  await page.clock.runFor(2000)
  await expect(page.getByText("prices — succeeded")).toBeVisible()

  // The refresh replaces the data, and the default would send the reader back to
  // page 1 at exactly the moment they asked for a fresher number.
  await expect(page.getByText("Showing 11 to 20 of 120 entries")).toBeVisible()

  // Choosing a different page size, on the other hand, does start from the top.
  await page.getByRole("combobox").click()
  await page.getByRole("option", { name: "25", exact: true }).click()
  await expect(page.getByText("Showing 1 to 25 of 120 entries")).toBeVisible()
})

test("only the latest balance request is shown, and results stay correlated", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/balance")
  await page.clock.install()
  await page.getByRole("button", { name: "LIVE", exact: true }).click()
  await page.getByRole("button", { name: "Check balance", exact: true }).click()
  await expect(page.getByText("balance — accepted")).toBeVisible()
  await page.getByRole("button", { name: "TESTNET", exact: true }).click()
  await page.getByRole("button", { name: "Check balance", exact: true }).click()
  // The newer request replaces the older section instead of adding one.
  await expect(page.getByTestId("command")).toHaveCount(1)
  const section = page.getByTestId("command")
  await expect(section).toContainText(secondId)
  await expect(section).toContainText("Requested balance network: TESTNET")

  // A result correlated to a different command is never accepted as this one.
  api.results.set(secondId, {
    command_id: firstId,
    type: "balance",
    ok: true,
    data: { wrong: true },
    at,
  })
  await page.clock.runFor(9000)
  await expect(section).toContainText("pending")
  await expect(section).not.toContainText("wrong")

  api.results.set(secondId, {
    command_id: secondId,
    type: "balance",
    ok: true,
    data: { USDT: 2 },
    at,
  })
  await page.clock.runFor(2000)
  await expect(section).toContainText("succeeded")
  await expect(section).toContainText('"USDT": 2')
  // The superseded request is still polled; the swap dropped nothing.
  expect(api.reads).toContain(firstId)
  expect(api.reads).toContain(secondId)
  expect(api.reads).not.toContain("MISSING")
})

test("balances never appear on the Strategy page", async ({ page }) => {
  const api = await mock(page)
  await page.goto("/balance")
  await page.clock.install()
  await page.getByRole("button", { name: "TESTNET", exact: true }).click()
  await page.getByRole("button", { name: "Check balance", exact: true }).click()
  api.results.set(firstId, {
    command_id: firstId,
    type: "balance",
    ok: true,
    data: { USDT: 777.5 },
    at,
  })
  await page.clock.runFor(2000)
  await expect(page.getByText(/777.5/)).toBeVisible()

  await page.getByRole("link", { name: "Strategy", exact: true }).click()
  await expect(page).toHaveURL(/\/strategy$/)
  // Neither the tracked balance command nor the heartbeat balances belong here.
  await expect(page.getByTestId("command")).toHaveCount(0)
  await expect(page.getByText("balance — succeeded")).toHaveCount(0)
  await expect(page.getByText(/777.5/)).toHaveCount(0)
  await expect(page.getByText(/Heartbeat balances/)).toHaveCount(0)
})

test("offline stop remains pending then times out without success", async ({
  page,
}) => {
  const status = snapshot()
  status.online = false
  status.desired.running = false
  const api = await mock(page, status)
  await page.goto("/strategy")
  await page.clock.install()
  await page.getByRole("button", { name: "Stop", exact: true }).click()
  await expect(page.getByText("stop — accepted")).toBeVisible()
  await page.clock.runFor(2000)
  await expect(page.getByText("stop — pending")).toBeVisible()
  await page.clock.runFor(121000)
  await expect(page.getByText("stop — timeout")).toBeVisible()
  await expect(page.getByText(/Execution outcome unknown/)).toBeVisible()
  expect(api.reads.every((id) => id === firstId)).toBe(true)
  await expect(page.getByText("stop — succeeded")).toHaveCount(0)
})

test("missing heartbeat and transient result errors never imply success", async ({
  page,
}) => {
  const status = snapshot()
  status.actual = null
  status.online = false
  const api = await mock(page, status)
  let unavailable = true
  await page.route("**/trading/result/**", async (route) => {
    if (unavailable)
      await route.fulfill({
        status: 503,
        json: { detail: "Redis unavailable" },
      })
    else await route.fallback()
  })
  await page.goto("/strategy")
  await expect(
    page.getByText("Actual network: unknown", { exact: true }),
  ).toBeVisible()
  await page.clock.install()
  // Testnet Start skips the live-only confirmation dialog.
  await page.getByRole("button", { name: "TESTNET", exact: true }).click()
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText("start — accepted")).toBeVisible()
  await page.clock.runFor(2000)
  await expect(
    page.getByText("Result temporarily unavailable; retrying"),
  ).toBeVisible()
  await expect(page.getByText("start — pending")).toBeVisible()
  unavailable = false
  api.results.set(firstId, { command_id: firstId, type: "start", ok: true, at })
  await page.clock.runFor(2000)
  await expect(page.getByText("start — succeeded")).toBeVisible()
  await expect(
    page.getByText("Result temporarily unavailable; retrying"),
  ).toHaveCount(0)
})

test("a command started on /strategy keeps polling and resolves after navigating to /balance", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/strategy")
  await page.clock.install()
  await page.getByRole("button", { name: "Stop", exact: true }).click()
  await expect(page.getByText("stop — accepted")).toBeVisible()
  await page.clock.runFor(2000)
  await expect(page.getByText("stop — pending")).toBeVisible()
  const pollsBefore = api.reads.filter((id) => id === firstId).length

  await page.getByRole("link", { name: "Balance", exact: true }).click()
  await expect(page).toHaveURL(/\/balance$/)
  // The stop belongs to the Strategy page, and is still tracked and polled
  // while we are away from it. Poll rather than sampling once: whether a tick
  // has already landed depends on the app's timers versus the fake clock.
  await expect(page.getByText("stop — pending")).toHaveCount(0)
  api.results.set(firstId, { command_id: firstId, type: "stop", ok: true, at })
  await page.clock.runFor(2000)
  await expect
    .poll(() => api.reads.filter((id) => id === firstId).length)
    .toBeGreaterThan(pollsBefore)

  await page.getByRole("link", { name: "Strategy", exact: true }).click()
  await expect(page).toHaveURL(/\/strategy$/)
  await expect(page.getByText("stop — succeeded")).toBeVisible()
})

test("invalid numeric, list and domain values block Start without sending a request", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/strategy")
  await page.getByRole("button", { name: "TESTNET", exact: true }).click()

  // Non-finite number.
  await page.getByLabel("Take profit %").fill("Infinity")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(/Take profit % must be/)).toBeVisible()
  expect(api.posts).toEqual([])
  await page.getByLabel("Take profit %").fill("10")

  // List with one bad element rejects the whole list, not just that element.
  await page.getByLabel("EMA tide periods").fill("5, oops, 34")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(
    page.getByText(/EMA tide periods must be a comma-separated list/),
  ).toBeVisible()
  expect(api.posts).toEqual([])

  // Domain rule: EMA periods must be strictly increasing.
  await page.getByLabel("EMA tide periods").fill("34, 13, 5")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(/strictly increasing/)).toBeVisible()
  expect(api.posts).toEqual([])
  await page.getByLabel("EMA tide periods").fill("5, 13, 34")

  // Domain rule: tide:wave:ripple ratio must be 16:4:1.
  await page.getByLabel("Wave interval").fill("2h")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(/ratio 16:4:1/)).toBeVisible()
  expect(api.posts).toEqual([])
})

test("live Start requires explicit confirmation; cancel sends nothing", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/strategy")
  // Touch the draft so Start isn't disabled as an unconscious repeat of the
  // already-confirmed running config.
  await page.getByRole("button", { name: "LIVE", exact: true }).click()
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "Confirm live Start" }),
  ).toBeVisible()
  await page.getByRole("button", { name: "Cancel", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "Confirm live Start" }),
  ).toHaveCount(0)
  expect(api.posts).toEqual([])
})

test("Stop sends an empty payload and never carries strategy settings", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/strategy")
  await page.getByRole("button", { name: "Stop", exact: true }).click()
  await expect(page.getByText("stop — accepted")).toBeVisible()
  expect(api.posts[0].path).toContain("/stop/")
  expect(api.posts[0].body).toBeFalsy()
})

test("desired/actual confirmed-running and stopped states render distinct control copy", async ({
  page,
}) => {
  const running = snapshot()
  await mock(page, running)
  await page.goto("/strategy")
  await expect(
    page.getByText("Confirmed running with the desired configuration."),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Start", exact: true }),
  ).toBeDisabled()

  const stopped = snapshot()
  stopped.desired.running = false
  stopped.actual!.state = "idle"
  await mock(page, stopped)
  await page.goto("/strategy")
  await expect(page.getByText("Confirmed stopped.")).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Stop", exact: true }),
  ).toBeDisabled()
})

test("a rejected session asks for a fresh login instead of half a menu", async ({
  page,
}) => {
  // Every way `GET /users/me` can refuse a stored session: 403/401 for a token
  // it cannot validate, 404 when the token names a user that no longer exists
  // (a recreated database — the case that showed "Oops!" on every page), 400
  // for a deactivated user.
  for (const status of [400, 401, 403, 404]) {
    await test.step(`GET /users/me answers ${status}`, async () => {
      await mock(page)
      await page.route("**/api/v1/users/me", (route) =>
        route.fulfill({ status, json: { detail: "rejected" } }),
      )
      await page.goto("/strategy")
      // Previously this rendered an error page with only Dashboard/Items in
      // the menu, because a present token was treated as a valid session.
      await expect(page).toHaveURL(/\/login$/)
      await expect(page.getByTestId("email-input")).toBeVisible()
      expect(
        await page.evaluate(() => localStorage.getItem("access_token")),
      ).toBeNull()
    })
  }
})

test("a server failure is not misreported as a signed-out session", async ({
  page,
}) => {
  for (const status of [500, 503]) {
    await mock(page)
    await page.route("**/api/v1/users/me", (route) =>
      route.fulfill({ status, json: { detail: "boom" } }),
    )
    await page.goto("/strategy")
    await expect(page).not.toHaveURL(/\/login$/)
    expect(
      await page.evaluate(() => localStorage.getItem("access_token")),
    ).toBe("mock-token")
  }
})

test("unpublished catalog shows a message and blocks Start instead of an empty form", async ({
  page,
}) => {
  const stopped = snapshot()
  stopped.desired.running = false
  stopped.actual!.state = "idle"
  const api = await mock(page, stopped, { available: false })
  await page.goto("/strategy")
  await expect(
    page.getByText(/has not published a strategy catalog/),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Start", exact: true }),
  ).toBeDisabled()
  await expect(page.getByLabel("Take profit %")).toHaveCount(0)
  expect(api.posts).toEqual([])
})
