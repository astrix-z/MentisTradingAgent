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
