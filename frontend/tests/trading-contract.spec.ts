import { expect, type Page, test } from "@playwright/test"
import type {
  CommandEnvelope,
  CommandResult,
  StrategyCatalog,
  TradingStatus,
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
async function mock(
  page: Page,
  status = snapshot(),
  published: StrategyCatalog = catalog(),
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
  return { posts, results, reads, status }
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
  await expect(page.getByLabel("Symbols", { exact: true })).toHaveValue(
    "ETHUSDT",
  )
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

test("parallel balance commands stay correlated beyond seven seconds", async ({
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
  await expect(page.getByTestId("command")).toHaveCount(2)
  api.results.set(firstId, {
    command_id: secondId,
    type: "balance",
    ok: true,
    data: { wrong: true },
    at,
  })
  api.results.set(secondId, {
    command_id: secondId,
    type: "balance",
    ok: true,
    data: { USDT: 2 },
    at,
  })
  await page.clock.runFor(9000)
  const first = page.getByTestId("command").filter({ hasText: firstId })
  const second = page.getByTestId("command").filter({ hasText: secondId })
  await expect(first).toContainText("pending")
  await expect(first).not.toContainText("wrong")
  await expect(second).toContainText("succeeded")
  await expect(second).toContainText("Requested balance network: TESTNET")
  api.results.set(firstId, {
    command_id: firstId,
    type: "balance",
    ok: true,
    data: { USDT: 1 },
    at,
  })
  await page.clock.runFor(2000)
  await expect(first).toContainText("succeeded")
  await expect(first).toContainText("Requested balance network: LIVE")
  expect(api.reads).not.toContain("MISSING")
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

  await page.getByRole("link", { name: "Balance", exact: true }).click()
  await expect(page).toHaveURL(/\/balance$/)
  // Still tracked after the route change; no duplicate poll for the same id.
  await expect(page.getByText("stop — pending")).toBeVisible()

  api.results.set(firstId, { command_id: firstId, type: "stop", ok: true, at })
  await page.clock.runFor(2000)
  await expect(page.getByText("stop — succeeded")).toBeVisible()
  expect(api.reads.filter((id) => id === firstId).length).toBeGreaterThan(0)

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
