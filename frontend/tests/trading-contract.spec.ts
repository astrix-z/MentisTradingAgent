import { expect, test } from "@playwright/test"
import type { TradingStatus } from "../src/client"
import strategyCatalog from "./fixtures/strategy-catalog.json" with {
  type: "json",
}
import {
  accepted,
  at,
  catalog,
  choose,
  expectBlocked,
  firstId,
  manySymbols,
  mock,
  secondId,
  snapshot,
} from "./utils/tradingMock"

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
  // Collapsed until asked for; expanding shows the amounts as a table.
  const heartbeatBalances = page.getByRole("table", {
    name: "Heartbeat balances",
  })
  await expect(heartbeatBalances).toBeHidden()
  await page.getByText("Heartbeat balances — LIVE (stale)").click()
  await expect(heartbeatBalances.getByRole("row").nth(1)).toHaveText(
    /USDT\s*123/,
  )
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
  await expect(page.getByText(accepted("balance"))).toBeVisible()
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
  // One input per EMA period (§4.5.6).
  for (const [index, period] of ["6", "14", "35"].entries())
    await expect(page.getByLabel(`EMA tide periods #${index + 1}`)).toHaveValue(
      period,
    )
  // Desired params that match no preset keep the selector on them.
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "— current launch params —",
  )
  // Old parameter names are read through the catalog's legacy_keys.
  await expect(page.getByLabel("Swing low WAVE N")).toHaveValue("7")
  await expect(page.getByLabel("Trail RIPPLE N")).toHaveValue("9")
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
  await expect(page.getByText(accepted("start"))).toBeVisible()
  // Decimals are sent as strings so precision survives the round-trip.
  expect(api.posts[0].body).toMatchObject({
    network: "live",
    symbols: ["ETHUSDT"],
    params: { tp_pct: "19", swing_low_wave_n: 7, trail_ripple_n: 9 },
  })
  // Only schema fields are sent: the backend rejects anything else.
  const sent = (api.posts[0].body as { params: Record<string, unknown> }).params
  for (const key of ["future_param", "swing_low_h1_n", "trail_m15_n"])
    expect(sent).not.toHaveProperty(key)
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
  await expect(page.getByText(accepted("symbols"))).toBeVisible()
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
  await expect(page.getByText(accepted("symbols"))).toBeVisible()
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
  await expect(page.getByText(accepted("symbols"))).toBeVisible()
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
  await expect(page.getByText(accepted("prices"))).toBeVisible()
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
  await expect(page.getByText(accepted("prices"))).toBeVisible()
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
  await expect(page.getByText(accepted("balance"))).toBeVisible()
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
    data: {
      network: "testnet",
      fetched_at: at,
      assets: [{ asset: "USDT", free: "2", locked: "0.5" }],
    },
    at,
  })
  await page.clock.runFor(2000)
  await expect(section).toContainText("succeeded")
  // Rendered as a table under the command header, not as raw JSON.
  const row = section.getByRole("table", { name: "Balance" }).getByRole("row")
  await expect(row.nth(0)).toHaveText(/Asset\s*Free\s*Locked/)
  await expect(row.nth(1)).toHaveText(/USDT\s*2\s*0\.5/)
  await expect(section.locator("pre")).toHaveCount(0)
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
  await expect(page.getByText(accepted("stop"))).toBeVisible()
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
  await expect(page.getByText(accepted("start"))).toBeVisible()
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

test("a succeeded start shows a summary with the symbol count, not the list", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/strategy")
  await page.clock.install()
  await page.getByRole("button", { name: "TESTNET", exact: true }).click()
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(accepted("start"))).toBeVisible()
  api.results.set(firstId, {
    command_id: firstId,
    type: "start",
    ok: true,
    data: {
      running: true,
      network: "testnet",
      strategy: "tripple_screen",
      symbols: ["AAVEUSDT", "ACEUSDT", "ZROUSDT"],
    },
    at,
  })
  await page.clock.runFor(2000)
  const section = page.getByTestId("command")
  await expect(section).toContainText("start — succeeded")
  await expect(section).toContainText(`Command ID: ${firstId}`)
  await expect(section).toContainText(/Running\s*yes/)
  await expect(section).toContainText(/Network\s*TESTNET/)
  await expect(section).toContainText(/Strategy\s*tripple_screen/)
  await expect(section).toContainText(/Symbols\s*3/)
  await expect(section).not.toContainText("AAVEUSDT")
  await expect(section.locator("pre")).toHaveCount(0)
})

test("a command started on /strategy keeps polling and resolves after navigating to /balance", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/strategy")
  await page.clock.install()
  await page.getByRole("button", { name: "Stop", exact: true }).click()
  await expect(page.getByText(accepted("stop"))).toBeVisible()
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
  await expectBlocked(page)
  await expect(page.getByText(/Take profit % must be/).first()).toBeVisible()
  expect(api.posts).toEqual([])
  await page.getByLabel("Take profit %").fill("10")

  // One bad EMA element is named by its position.
  await page.getByLabel("EMA tide periods #2").fill("oops")
  await expectBlocked(page)
  await expect(
    page.getByText("EMA tide periods #2 must be a whole number").first(),
  ).toBeVisible()
  expect(api.posts).toEqual([])

  // Domain rule: EMA periods must be strictly increasing.
  await page.getByLabel("EMA tide periods #1").fill("34")
  await page.getByLabel("EMA tide periods #2").fill("13")
  await page.getByLabel("EMA tide periods #3").fill("5")
  await expectBlocked(page)
  await expect(page.getByText(/strictly increasing/)).toBeVisible()
  expect(api.posts).toEqual([])
  await page.getByLabel("EMA tide periods #1").fill("5")
  await page.getByLabel("EMA tide periods #3").fill("34")

  // Domain rule: timeframes must satisfy tide > wave > ripple.
  // The live check shows it without a click, and a known-invalid draft
  // keeps Start disabled.
  await choose(page, "Wave interval", "H4")
  await expect(page.getByText(/tide > wave > ripple/)).toBeVisible()
  await expectBlocked(page)
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
  await expect(page.getByText(accepted("stop"))).toBeVisible()
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

// --- Presets, option lists and the live check (PRESET-07, plan §4.5, §7.1) ---

/** A stopped agent: Start is a fresh launch, not a repeat of a running one. */
function stopped(): TradingStatus {
  const state = snapshot()
  state.desired.running = false
  state.desired.network = "testnet"
  state.actual!.state = "idle"
  return state
}

const fastParams = strategyCatalog.strategies[0].presets[1].params

function sentParams(api: { posts: { body: unknown }[] }, index = 0) {
  return (api.posts[index].body as { params: Record<string, unknown> }).params
}

test("Fast fills every setting, an edit marks it modified, and Save as keeps it", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  await page.goto("/strategy")
  await choose(page, "Preset", "Fast · built-in")
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in",
  )
  await expect(page.getByLabel("Take profit %")).toHaveValue("5")
  await expect(page.getByLabel("Ripple interval")).toHaveText("M5")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(accepted("start"))).toBeVisible()
  // All 22 editable settings of the preset and the pinned volume window;
  // nothing from the previous desired params (old names, future_param).
  expect(Object.keys(fastParams)).toHaveLength(22)
  expect(sentParams(api)).toEqual({ ...fastParams, volume_window: "24h" })

  await page.getByLabel("Take profit %").fill("6")
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in (modified)",
  )
  // `5.0` is the same value as `5`: not a modification.
  await page.getByLabel("Take profit %").fill("5.0")
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in",
  )
  await page.getByLabel("Take profit %").fill("6")

  // A built-in preset is read-only.
  await expect(page.getByRole("button", { name: "Update" })).toHaveCount(0)
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0)
  await page.getByRole("button", { name: "Save as…" }).click()
  await page.getByLabel("Preset name").fill("My fast")
  await page.getByRole("button", { name: "Save", exact: true }).click()
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText("My fast")
  expect(api.presets.writes[0]).toMatchObject({
    method: "POST",
    body: {
      name: "My fast",
      params: { ...fastParams, tp_pct: "6", volume_window: "24h" },
    },
  })

  await page.reload()
  await choose(page, "Preset", "My fast")
  await expect(page.getByLabel("Take profit %")).toHaveValue("6")
  await page.getByLabel("Take profit %").fill("7")
  await page.getByRole("button", { name: "Update", exact: true }).click()
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText("My fast")
  expect(api.presets.writes[1]).toMatchObject({
    method: "PUT",
    body: { name: "My fast", params: { tp_pct: "7" } },
  })
  await page.getByRole("button", { name: "Delete", exact: true }).click()
  await page.getByRole("button", { name: "Delete preset" }).click()
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "— current launch params —",
  )
  expect(api.presets.writes[2].method).toBe("DELETE")
  await page.getByLabel("Preset", { exact: true }).click()
  await expect(page.getByRole("option", { name: "My fast" })).toHaveCount(0)
})

test("without a snapshot the form starts from Default; a matching snapshot selects its preset", async ({
  page,
}) => {
  const empty = stopped()
  empty.desired.params = {}
  empty.desired.strategy = null
  await mock(page, empty)
  await page.goto("/strategy")
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Default · built-in",
  )

  const onFast = stopped()
  // Written before the rename, and with a decimal as a JSON number.
  const { swing_low_wave_n, trail_ripple_n, ...rest } = fastParams
  onFast.desired.params = {
    ...rest,
    tp_pct: 5,
    swing_low_h1_n: swing_low_wave_n,
    trail_m15_n: trail_ripple_n,
  }
  await mock(page, onFast)
  await page.goto("/strategy")
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in",
  )
})

test("a click on Start before the live check runs still checks the exact draft", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  await page.clock.install()
  await page.goto("/strategy")
  await expect(
    page.getByRole("button", { name: "Start", exact: true }),
  ).toBeEnabled()
  // Hold the debounce timer: only the check on the click can catch it.
  await page.clock.pauseAt(Date.now() + 60_000)
  await page.getByLabel("Take profit %").fill("0")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(
    page.getByText("Take profit % must be greater than 0").first(),
  ).toBeVisible()
  expect(api.posts).toEqual([])
})

test("changed option lists cancel a live Start awaiting confirmation", async ({
  page,
}) => {
  const api = await mock(page)
  await page.clock.install()
  await page.goto("/strategy")
  await page.getByRole("button", { name: "LIVE", exact: true }).click()
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(
    page.getByRole("heading", { name: "Confirm live Start" }),
  ).toBeVisible()
  api.presets.options.quote_asset = ["USDT"]
  await page.clock.runFor(31_000)
  await expect(
    page.getByRole("heading", { name: "Confirm live Start" }),
  ).toHaveCount(0)
  await expect(
    page.getByText(/awaiting confirmation; it was cancelled/),
  ).toBeVisible()
  expect(api.posts).toEqual([])
})

test("invalid desired params show their errors without an edit and keep Start off", async ({
  page,
}) => {
  const state = stopped()
  state.desired.params = { ...fastParams, tp_pct: "0", ripple_interval: "1M" }
  const api = await mock(page, state)
  await page.goto("/strategy")
  await expect(
    page.getByText("Take profit % must be greater than 0").first(),
  ).toBeVisible()
  await expect(
    page.getByText("Ripple interval: the agent does not support 1M").first(),
  ).toBeVisible()
  await expect(page.getByLabel("Ripple interval")).toHaveText(
    "1M — not in the list",
  )
  await expectBlocked(page)
  expect(api.posts).toEqual([])
})

test("option lists that fail to load make the check unavailable", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  api.presets.optionsStatus = 503
  await page.goto("/strategy")
  await expect(page.getByText(/option lists could not be loaded/)).toBeVisible({
    timeout: 20_000,
  })
  await expectBlocked(page)
  await expect(page.getByRole("button", { name: "Save as…" })).toBeDisabled()
})

test("an option removed from a list invalidates the preset that uses it", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  api.presets.options.tide_interval = [
    "1M",
    ...api.presets.options.tide_interval,
  ]
  await page.clock.install()
  await page.goto("/strategy")
  await choose(page, "Preset", "Fast · built-in")
  await expect(page.getByLabel("Ripple interval")).toHaveText("M5")

  api.presets.options.ripple_interval =
    api.presets.options.ripple_interval.filter((option) => option !== "5m")
  await page.clock.runFor(31_000)
  await expect(
    page.getByText("Ripple interval: 5m is not in the option list").first(),
  ).toBeVisible()
  await expect(page.getByLabel("Ripple interval")).toHaveText(
    "M5 — not in the list",
  )
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in (invalid)",
  )
  await expectBlocked(page)
  expect(api.posts).toEqual([])

  // Weekly and daily are offered for the tide; a listed value the agent does
  // not support is shown but cannot be chosen.
  await page.getByLabel("Tide interval").click()
  for (const option of ["W1", "D1"])
    await expect(
      page.getByRole("option", { name: option, exact: true }),
    ).toBeEnabled()
  await expect(
    page.getByRole("option", { name: "1M — not supported by the agent" }),
  ).toHaveAttribute("aria-disabled", "true")
})

test("required history is shown per timeframe and warns past a year without blocking", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  await page.goto("/strategy")
  await choose(page, "Preset", "Default · built-in")
  const tide = page.locator('[data-history="tide_interval"]')
  // The minimum window the constraints allow is shown all the time, not only
  // after a too small window was typed.
  await expect(tide).toHaveText(
    "History: 166.7 days · minimum 35 bars (5.8 days)",
  )
  await expect(page.locator('[data-history="wave_interval"]')).toContainText(
    "History: 41.7 days · minimum 35 bars",
  )
  await expect(page.locator('[data-history="ripple_interval"]')).toContainText(
    "History: 10.4 days · minimum 35 bars",
  )
  await choose(page, "Tide interval", "D1")
  await expect(tide).toContainText("1000 days — more than a year")
  await expect(tide).toContainText("minimum 35 bars (35 days)")
  await choose(page, "Tide interval", "W1")
  await expect(tide).toContainText("7000 days — more than a year")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(accepted("start"))).toBeVisible()
  expect(sentParams(api).tide_interval).toBe("1w")
})

test("a dirty draft survives a new schema: renamed keys move, a banner says so", async ({
  page,
}) => {
  // The catalog of an agent from before the rename: old names, no legacy_keys.
  const old = JSON.parse(
    JSON.stringify(strategyCatalog)
      .replaceAll("swing_low_wave_n", "swing_low_h1_n")
      .replaceAll("trail_ripple_n", "trail_m15_n"),
  )
  delete old.strategies[0].legacy_keys
  old.strategies[0].presets = []
  old.schema_hash = "before-the-rename"
  const published = catalog(old)
  const api = await mock(page, stopped(), published)
  api.presets.list.items = []
  await page.clock.install()
  await page.goto("/strategy")
  await expect(page.getByLabel("Swing low WAVE N")).toHaveValue("7")
  await page.getByLabel("Take profit %").fill("19")

  Object.assign(published, catalog())
  await page.clock.runFor(61_000)
  await expect(page.getByText(/settings schema changed/)).toBeVisible()
  await expect(page.getByLabel("Take profit %")).toHaveValue("19")
  await expect(page.getByLabel("Swing low WAVE N")).toHaveValue("7")
  await expect(page.getByLabel("Trail RIPPLE N")).toHaveValue("9")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(accepted("start"))).toBeVisible()
  const sent = sentParams(api)
  expect(sent).toMatchObject({
    tp_pct: "19",
    swing_low_wave_n: 7,
    trail_ripple_n: 9,
  })
  for (const key of ["swing_low_h1_n", "trail_m15_n", "future_param"])
    expect(sent).not.toHaveProperty(key)
})

test("a partial built-in sync is reported with its errors", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  api.presets.list.builtin_sync = {
    status: "partial",
    errors: [{ index: 1, name: "Fast", type: "name_conflict" }],
  }
  await page.goto("/strategy")
  await expect(
    page.getByText("Some built-in presets from the agent were not accepted:"),
  ).toBeVisible()
  await expect(page.getByText("Fast: name_conflict")).toBeVisible()
})

test("indicators monitor reads on request and renders the strategy's own groups", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/indicators")
  await page.clock.install()
  await expect(
    page.getByRole("heading", { name: "Strategy Indicators Monitor" }),
  ).toBeVisible()
  // Nothing is requested until the user asks.
  expect(api.posts).toHaveLength(0)
  await page.getByRole("button", { name: "Refresh", exact: true }).click()
  await expect(page.getByRole("button", { name: "Refreshing…" })).toBeVisible()
  expect(api.posts).toEqual([
    { path: "/api/v1/trading/indicators/", body: null },
  ])

  api.results.set(firstId, {
    command_id: firstId,
    type: "indicators",
    ok: true,
    at,
    data: {
      strategy: "tripple_screen",
      network: "live",
      computed_at: "2026-09-23T08:31:00+00:00",
      groups: [
        {
          key: "tide",
          label: "Tide",
          interval: "4h",
          indicators: [{ key: "ema_fast", label: "EMA 5" }],
        },
        {
          key: "wave",
          label: "Wave",
          interval: "1h",
          indicators: [{ key: "force_index", label: "FI 2" }],
        },
      ],
      rows: [
        {
          symbol: "PEPEUSDT",
          groups: {
            tide: {
              bar_open_time: "2026-09-23T04:00:00+00:00",
              values: {
                ema_fast: { current: "0.0000123456789", previous: "0.0000120" },
              },
            },
            wave: {
              bar_open_time: "2026-09-23T07:00:00+00:00",
              values: {
                force_index: { current: "123456789.5", previous: "-2500.25" },
              },
            },
          },
        },
        {
          symbol: "BTCUSDT",
          groups: {
            tide: {
              bar_open_time: "2026-09-23T04:00:00+00:00",
              values: {
                ema_fast: { current: "64210.53219", previous: "64100" },
              },
            },
            // A lagging symbol: its own bar is stamped in the cell.
            wave: {
              bar_open_time: "2026-09-23T06:00:00+00:00",
              values: { force_index: { current: "0", previous: null } },
            },
          },
        },
      ],
    },
  })
  await page.clock.runFor(2000)

  const table = page.getByRole("table")
  await expect(table).toContainText("Tide 4h")
  await expect(table).toContainText("bar 2026-09-23 04:00 UTC")
  await expect(table).toContainText("EMA 5")
  await expect(table).toContainText("FI 2")
  const pepe = page.getByRole("row", { name: /PEPEUSDT/ })
  await expect(pepe).toContainText("1.23457e-5")
  await expect(pepe).toContainText("1.23457e+8")
  await expect(pepe).toContainText("prev -2500.25")
  const btc = page.getByRole("row", { name: /BTCUSDT/ })
  await expect(btc).toContainText("64210.5")
  await expect(btc).toContainText("bar 2026-09-23 06:00 UTC")
  await expect(btc).toContainText("prev —")
  await expect(page.getByTestId("computed-at")).toContainText(
    "2026-09-23 08:31 UTC",
  )
  await expect(page.getByTestId("snapshot-source")).toHaveText(
    " · read from tripple_screen · LIVE",
  )
  await expect(page.getByTestId("snapshot-mismatch")).toHaveCount(0)

  // Review 2026-09-30: the agent switches to TESTNET and the status poll
  // picks it up, but nobody refreshed: the LIVE values must not pass for
  // TESTNET ones.
  api.status.actual!.network = "testnet"
  await page.clock.runFor(6000)
  await expect(page.getByText(/Agent: .* · TESTNET/)).toBeVisible()
  await expect(page.getByTestId("snapshot-source")).toContainText("LIVE")
  await expect(page.getByTestId("snapshot-mismatch")).toContainText(
    "read from tripple_screen on LIVE; the agent now runs tripple_screen on TESTNET",
  )
})

test("indicators monitor explains a failed read instead of an empty table", async ({
  page,
}) => {
  const api = await mock(page)
  await page.goto("/indicators")
  await page.clock.install()
  await page.getByRole("button", { name: "Refresh", exact: true }).click()
  api.results.set(firstId, {
    command_id: firstId,
    type: "indicators",
    ok: false,
    message: "no running strategy is publishing indicators",
    at,
  })
  await page.clock.runFor(2000)
  await expect(page.getByRole("alert")).toContainText(
    "no running strategy is publishing indicators",
  )
  await expect(page.getByRole("table")).toHaveCount(0)
})

test("indicators monitor explains an empty table with the last screening", async ({
  page,
}) => {
  // Stage 2026-10-07: testnet was reset, every symbol lacked tide history and
  // the page only said that nothing was selected.
  const api = await mock(page)
  await page.goto("/indicators")
  await page.clock.install()
  await page.getByRole("button", { name: "Refresh", exact: true }).click()
  api.results.set(firstId, {
    command_id: firstId,
    type: "indicators",
    ok: true,
    at,
    data: {
      strategy: "tripple_screen",
      network: "testnet",
      computed_at: "2026-10-07T19:23:53+00:00",
      groups: [],
      rows: [],
      screening: {
        at: "2026-10-07T19:00:05+00:00",
        universe: 3,
        selected: 0,
        skipped: [
          {
            symbol: "ADAUSDT",
            reason: "not enough 1h history: 9 of 40 closed bars",
          },
          {
            symbol: "BTCUSDT",
            reason: "not enough 1h history: 9 of 40 closed bars",
          },
          { symbol: "ETHUSDT", reason: "tide EMA 13 / EMA 34 not both rising" },
        ],
      },
    },
  })
  await page.clock.runFor(2000)
  const screening = page.getByTestId("screening")
  await expect(screening).toContainText(
    "0 of 3 symbols selected for work at the last screening (2026-10-07 19:00 UTC)",
  )
  await expect(screening).toContainText(
    "ADAUSDT, BTCUSDT: not enough 1h history: 9 of 40 closed bars",
  )
  await expect(screening).toContainText(
    "ETHUSDT: tide EMA 13 / EMA 34 not both rising",
  )
  await expect(page.getByText("No symbols are selected")).toHaveCount(0)
  await expect(page.getByRole("table")).toHaveCount(0)
})

// --- Option list editor (PRESET-08, plan §4.4, §4.5.7) ---

test("the list editor normalizes entries, keeps unsupported ones (D3) and saves regardless of the form", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  await page.goto("/strategy")
  // An invalid form does not block saving a list.
  await page.getByLabel("Take profit %").fill("0")
  await expectBlocked(page)

  const toggle = page.getByRole("button", { name: "Edit option lists" })
  await expect(toggle).toHaveAttribute("aria-expanded", "false")
  await expect(page.getByLabel("Add to Wave interval")).toHaveCount(0)
  await toggle.click()
  const wave = page.getByRole("region", { name: "Wave interval options" })
  const add = async (text: string) => {
    await wave.getByLabel("Add to Wave interval").fill(text)
    await wave.getByRole("button", { name: "Add", exact: true }).click()
  }
  // A label and its code are one value.
  await add("H1")
  await expect(wave.getByText("1h is already in the list.")).toBeVisible()
  await add(" 1h ")
  await expect(wave.getByText("1h is already in the list.")).toBeVisible()
  await add("W1")
  await expect(wave.locator('[data-option="1w"]')).toContainText("W1")
  await expect(wave.locator('[data-option="1w"]')).not.toHaveAttribute(
    "data-unsupported",
  )
  // Kept as typed and marked: `1M` is a month, not `1m`.
  await add("custom")
  await add("1M")
  for (const value of ["custom", "1M"])
    await expect(wave.locator(`[data-option="${value}"]`)).toHaveAttribute(
      "data-unsupported",
      "true",
    )
  await expect(
    wave.getByText(/Not supported by the agent: custom, 1M/),
  ).toBeVisible()
  // Order is the list's own: W1 moved to the front.
  for (let step = 0; step < 9; step += 1)
    await wave.getByRole("button", { name: "Move W1 earlier" }).click()
  await wave.getByRole("button", { name: "Save Wave interval" }).click()
  await expect(wave.getByText("Saved.")).toBeVisible()
  expect(api.presets.writes.at(-1)).toEqual({
    method: "PUT",
    path: "options/wave_interval",
    body: {
      options: [
        "1w",
        "1d",
        "12h",
        "6h",
        "4h",
        "2h",
        "1h",
        "30m",
        "15m",
        "5m",
        "custom",
        "1M",
      ],
    },
  })
  // The select offers the saved list: an unsupported entry is disabled.
  await page.getByLabel("Wave interval", { exact: true }).click()
  await expect(
    page.getByRole("option", { name: "custom — not supported by the agent" }),
  ).toHaveAttribute("aria-disabled", "true")
  await expect(
    page.getByRole("option", { name: "W1", exact: true }),
  ).toBeEnabled()
})

test("removing M5 in the editor invalidates Fast, with a warning before saving", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  await page.goto("/strategy")
  await choose(page, "Preset", "Fast · built-in")
  await page.getByRole("button", { name: "Edit option lists" }).click()
  const ripple = page.getByRole("region", { name: "Ripple interval options" })
  await ripple.getByRole("button", { name: "Remove M5" }).click()
  await expect(
    ripple.getByText(
      "Presets using a removed value will become invalid: Fast.",
    ),
  ).toBeVisible()
  await ripple.getByRole("button", { name: "Save Ripple interval" }).click()
  await expect(ripple.getByText("Saved.")).toBeVisible()
  expect(api.presets.options.ripple_interval).not.toContain("5m")
  await expect(
    page.getByText("Ripple interval: 5m is not in the option list").first(),
  ).toBeVisible()
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in (invalid)",
  )
  await expectBlocked(page)
  expect(api.posts).toEqual([])
})

test("an emptied list cannot be saved and a discarded edit restores it", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  await page.goto("/strategy")
  await page.getByRole("button", { name: "Edit option lists" }).click()
  const quote = page.getByRole("region", { name: "Quote asset options" })
  await quote.getByRole("button", { name: "Remove USDT" }).click()
  await quote.getByRole("button", { name: "Remove USDC" }).click()
  await expect(quote.getByText("A list cannot be empty.")).toBeVisible()
  await expect(
    quote.getByRole("button", { name: "Save Quote asset" }),
  ).toBeDisabled()
  await quote.getByRole("button", { name: "Discard" }).click()
  await expect(quote.locator("[data-option]")).toHaveText(["USDT", "USDC"])
  expect(api.presets.writes).toEqual([])
})

test("a required choice with no default is not invented for an old preset", async ({
  page,
}) => {
  // Review 2026-09-30 (PRESET-07): the parser picked the first option.
  const published = structuredClone(strategyCatalog)
  const ripple = published.strategies[0].settings_schema.properties
    .ripple_interval as { default?: string }
  delete ripple.default
  const status = stopped()
  delete (status.desired.params as Record<string, unknown>).ripple_interval
  const api = await mock(page, status, catalog(published))
  await page.goto("/strategy")
  await expect(
    page.locator(
      '[data-issue-type="required"][data-issue-field="ripple_interval"]',
    ),
  ).toBeVisible()
  await expectBlocked(page)
  expect(api.posts).toEqual([])
})

test("an unresolved entry is released only after confirmation, and a refusal is shown", async ({
  page,
}) => {
  const cid = "mta-0123456789abcdef01234567-e1"
  const status = snapshot()
  // The Unresolved entry card appears only while the agent reports one.
  const api = await mock(page, status)
  await page.goto("/strategy")
  await expect(page.getByText("Unresolved entry")).toHaveCount(0)

  status.actual = {
    ...status.actual!,
    execution: {
      mode: "enabled",
      leader: true,
      open_positions: 0,
      alerts: {
        unknown: `order ${cid} (BTCUSDT) outcome unknown for 300s; not retried`,
      },
    },
  }
  await page.reload()
  await page.clock.install()
  const card = page.getByRole("region", { name: "Release entry" })
  await expect(card.getByRole("alert")).toContainText(cid)
  const input = card.getByLabel("Entry client order id")
  await expect(input).toHaveValue(cid)

  // Not an entry id: the button stays off.
  await input.fill("web_manual_1")
  await expect(card.getByText(/Not an entry client id/)).toBeVisible()
  await expect(
    card.getByRole("button", { name: "Release entry" }),
  ).toBeDisabled()
  await input.fill(cid)

  // Cancel sends nothing.
  await card.getByRole("button", { name: "Release entry" }).click()
  await page.getByRole("button", { name: "Cancel" }).click()
  expect(api.posts).toHaveLength(0)

  await card.getByRole("button", { name: "Release entry" }).click()
  await page.getByRole("button", { name: "Release", exact: true }).click()
  await expect(card.getByText(accepted("release_entry"))).toBeVisible()
  expect(api.posts).toEqual([
    {
      path: "/api/v1/trading/commands/",
      body: { type: "release_entry", payload: { client_order_id: cid } },
    },
  ])

  // The agent refuses: the message is shown, success is not implied.
  api.results.set(firstId, {
    command_id: firstId,
    type: "release_entry",
    ok: false,
    message: "not released: BUY order 9 (no client id) may be ours",
    at,
  })
  await page.clock.runFor(2000)
  await expect(card.getByText("release_entry — failed")).toBeVisible()
  await expect(card.getByText(/may be ours/)).toBeVisible()
})

// --- Review 2026-10-03 (PRESET-07/08) ---

test("a preset invalid only for a missing option is valid again once the option is back", async ({
  page,
}) => {
  const api = await mock(page, stopped())
  api.presets.options.ripple_interval =
    api.presets.options.ripple_interval.filter((option) => option !== "5m")
  const fast = api.presets.list.items[1]
  fast.valid = false
  fast.errors = [{ field: "ripple_interval", message: "5m is not in the list" }]
  let presetReads = 0
  page.on("request", (request) => {
    if (
      request.method() === "GET" &&
      request.url().endsWith("/tripple_screen/presets/")
    )
      presetReads += 1
  })
  await page.clock.install()
  await page.goto("/strategy")
  await choose(page, "Preset", "Fast · built-in (invalid)")
  await expectBlocked(page)
  const readsBefore = presetReads

  // Another administrator puts M5 back; the server re-checks the preset.
  api.presets.options.ripple_interval = [
    ...api.presets.options.ripple_interval,
    "5m",
  ]
  fast.valid = true
  fast.errors = []
  await page.clock.runFor(31_000)
  await expect(page.getByLabel("Ripple interval")).toHaveText("M5")
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in",
  )
  expect(presetReads).toBeGreaterThan(readsBefore)
  await expect(
    page.getByRole("button", { name: "Start", exact: true }),
  ).toBeEnabled()
})

test("presets read without a catalog are read again when the catalog first appears", async ({
  page,
}) => {
  // Re-review 2026-10-03: the first unavailable → ready transition did not
  // read the presets again; the stale built-in Fast (tp 5) stayed.
  const published = catalog({ available: false })
  const api = await mock(page, stopped(), published)
  let presetReads = 0
  page.on("request", (request) => {
    if (
      request.method() === "GET" &&
      request.url().endsWith("/tripple_screen/presets/")
    )
      presetReads += 1
  })
  await page.clock.install()
  await page.goto("/strategy")
  await expect(
    page.getByText(/has not published a strategy catalog/),
  ).toBeVisible()
  await expect.poll(() => presetReads).toBe(1)

  // The agent publishes its catalog, whose Fast takes profit at 7.
  const body = structuredClone(strategyCatalog)
  body.strategies[0].presets[1].params.tp_pct = "7"
  Object.assign(published, catalog(body))
  api.presets.list.items[1].params.tp_pct = "7"
  await page.clock.runFor(61_000)
  await expect(page.getByLabel("Take profit %")).toBeVisible()
  await expect.poll(() => presetReads).toBe(2)
  await choose(page, "Preset", "Fast · built-in")
  await expect(page.getByLabel("Take profit %")).toHaveValue("7")
  await page.clock.runFor(61_000)
  expect(presetReads).toBe(2) // read once more, not again and again
})

test("without a catalog the option lists stay editable and unknown support is said", async ({
  page,
}) => {
  const api = await mock(page, stopped(), { available: false })
  // Without a catalog the server cannot tell what the agent supports.
  await page.route("**/tripple_screen/options/", async (route) => {
    if (route.request().method() !== "GET") return route.fallback()
    await route.fulfill({
      json: {
        ripple_interval: { options: ["5m", "custom"], unsupported: null },
      },
    })
  })
  await page.goto("/strategy")
  await expect(
    page.getByText(/has not published a strategy catalog/),
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Start", exact: true }),
  ).toBeDisabled()
  const toggle = page.getByRole("button", { name: "Edit option lists" })
  await toggle.click()
  const ripple = page.getByRole("region", { name: "ripple_interval options" })
  await expect(ripple.locator('[data-option="custom"]')).toBeVisible()
  await expect(
    ripple.getByText(/not known which of these values the agent supports/),
  ).toBeVisible()
  expect(api.posts).toEqual([])
})

test("without a catalog an interval list still normalizes and refuses duplicates", async ({
  page,
}) => {
  // Re-review 2026-10-03: with no schema field the entry was only trimmed;
  // H1 joined 1h and Save sent a duplicate the backend refuses (422).
  const api = await mock(page, stopped(), { available: false })
  await page.route("**/tripple_screen/options/", async (route) => {
    if (route.request().method() !== "GET") return route.fallback()
    await route.fulfill({
      json: {
        wave_interval: { options: ["1h"], unsupported: null },
        quote_asset: { options: ["USDT"], unsupported: null },
      },
    })
  })
  await page.goto("/strategy")
  await page.getByRole("button", { name: "Edit option lists" }).click()
  const wave = page.getByRole("region", { name: "wave_interval options" })
  const add = async (text: string) => {
    await wave.getByLabel("Add to wave_interval").fill(text)
    await wave.getByRole("button", { name: "Add", exact: true }).click()
  }
  await add("H1")
  await expect(wave.getByText("1h is already in the list.")).toBeVisible()
  await expect(wave.locator('[data-option="1h"]')).toContainText("H1")
  await add("1M") // a month: kept as typed, not 1m
  await add("M15")
  await wave.getByRole("button", { name: "Save wave_interval" }).click()
  await expect(wave.getByText("Saved.")).toBeVisible()
  expect(api.presets.writes.at(-1)).toEqual({
    method: "PUT",
    path: "options/wave_interval",
    body: { options: ["1h", "1M", "15m"] },
  })

  // quote_asset is only trimmed: "h1" stays a value of its own.
  const quote = page.getByRole("region", { name: "quote_asset options" })
  await quote.getByLabel("Add to quote_asset").fill(" H1 ")
  await quote.getByRole("button", { name: "Add", exact: true }).click()
  await expect(quote.locator('[data-option="H1"]')).toBeVisible()
})
