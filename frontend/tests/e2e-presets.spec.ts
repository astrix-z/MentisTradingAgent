// End-to-end scenarios of plan §7.1 (UI, PRESET-07/08) against a real stack:
// backend + PostgreSQL + Redis with the catalog the agent publishes. Nothing
// is mocked; the Redis state an agent would write (runtime snapshot, symbol
// universe) is seeded with redis-cli. Skipped unless E2E_API_URL is set:
//
//   E2E_API_URL=http://localhost:18000 E2E_REDIS_CONTAINER=<name>
//   E2E_USER=… E2E_PASSWORD=… PLAYWRIGHT_BASE_URL=<vite with VITE_API_URL>
import { execFileSync } from "node:child_process"
import {
  type APIRequestContext,
  expect,
  type Page,
  test,
} from "@playwright/test"
import { choose, expectBlocked } from "./utils/tradingMock"

const API = process.env.E2E_API_URL
const REDIS = process.env.E2E_REDIS_CONTAINER ?? ""
const STRATEGY = "tripple_screen"
const BASE = `${API}/api/v1/trading/strategies/${STRATEGY}`

test.skip(!API, "e2e: needs a running stack (E2E_API_URL)")
test.describe.configure({ mode: "serial" })

function redis(...args: string[]): string {
  return execFileSync("docker", ["exec", REDIS, "redis-cli", ...args], {
    encoding: "utf8",
  }).trim()
}

function redisSet(key: string, value: unknown) {
  execFileSync("docker", ["exec", "-i", REDIS, "redis-cli", "-x", "SET", key], {
    input: JSON.stringify(value),
  })
}

function runtime(): { running: boolean; params: Record<string, unknown> } {
  return JSON.parse(redis("GET", "agent:runtime"))
}

let token = ""
let baselineLists: Record<string, string[]> = {}

async function api(request: APIRequestContext) {
  const headers = { Authorization: `Bearer ${token}` }
  return {
    get: async (path: string) =>
      (await request.get(`${BASE}/${path}`, { headers })).json(),
    put: (path: string, data: unknown) =>
      request.put(`${BASE}/${path}`, { headers, data }),
    delete: (path: string) => request.delete(`${BASE}/${path}`, { headers }),
  }
}

/** A stopped agent that marked BTCUSDT, no saved presets of our own, the
 * option lists the migration seeds. */
async function reset(request: APIRequestContext, desired: object = {}) {
  const client = await api(request)
  for (const [field, options] of Object.entries(baselineLists))
    expect((await client.put(`options/${field}`, { options })).ok()).toBe(true)
  const { items } = await client.get("presets/")
  for (const preset of items as { id: string; is_builtin: boolean }[])
    if (!preset.is_builtin) await client.delete(`presets/${preset.id}`)
  redisSet("agent:runtime", {
    running: false,
    network: "testnet",
    strategy: STRATEGY,
    symbols: ["BTCUSDT"],
    params: {},
    updated_at: new Date().toISOString(),
    revision: 1,
    ...desired,
  })
  redisSet("agent:symbols", {
    available: true,
    quote_asset: "USDT",
    generated_at: new Date().toISOString(),
    total: 1,
    usable: 1,
    selected: 1,
    selected_unusable: 0,
    symbols: [
      {
        symbol: "BTCUSDT",
        base_asset: "BTC",
        quote_asset: "USDT",
        status: "TRADING",
        spot_allowed: true,
        quote_volume_24h: "1000",
        classification: "crypto",
        classification_label: null,
        excluded_reason: null,
        selected: true,
      },
    ],
  })
}

async function open(page: Page) {
  await page.addInitScript((value) => {
    localStorage.setItem("access_token", value)
  }, token)
  await page.goto("/strategy")
  await expect(page.getByLabel("Preset", { exact: true })).toBeVisible()
}

test.beforeAll(async ({ request }) => {
  const login = await request.post(`${API}/api/v1/login/access-token`, {
    form: {
      username: process.env.E2E_USER ?? "",
      password: process.env.E2E_PASSWORD ?? "",
    },
  })
  expect(login.ok()).toBe(true)
  token = (await login.json()).access_token
  const client = await api(request)
  const lists = await client.get("options/")
  baselineLists = Object.fromEntries(
    Object.entries(lists as Record<string, { options: string[] }>).map(
      ([field, entry]) => [field, entry.options],
    ),
  )
})

test.afterAll(async ({ request }) => {
  await reset(request)
})

test("Fast goes to the agent whole; Save as survives a reload; reserved names are refused", async ({
  page,
  request,
}) => {
  await reset(request)
  const client = await api(request)
  const fast = (await client.get("presets/")).items.find(
    (preset: { name: string }) => preset.name === "Fast",
  )
  expect(fast.valid).toBe(true)
  await open(page)
  await choose(page, "Preset", "Fast · built-in")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(/^start — (accepted|pending)$/)).toBeVisible()
  // The backend accepted and stored exactly the preset plus the const.
  await expect.poll(() => runtime().running).toBe(true)
  expect(Object.keys(fast.params)).toHaveLength(22)
  expect(runtime().params).toEqual({ ...fast.params, volume_window: "24h" })

  await page.getByLabel("Take profit %").fill("6")
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in (modified)",
  )
  await page.getByRole("button", { name: "Save as…" }).click()
  await page.getByLabel("Preset name").fill(" fast ")
  await page.getByRole("button", { name: "Save", exact: true }).click()
  await expect(page.getByText(/reserved for a built-in preset/)).toBeVisible()
  await page.getByLabel("Preset name").fill("E2E fast")
  await page.getByRole("button", { name: "Save", exact: true }).click()
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "E2E fast",
  )
  await page.reload()
  await choose(page, "Preset", "E2E fast")
  await expect(page.getByLabel("Take profit %")).toHaveValue("6")
  await expect(page.getByLabel("Ripple interval")).toHaveText("M5")
})

test("desired params under old names reach the backend under new names only", async ({
  page,
  request,
}) => {
  const client = await api(request)
  const { params } = (await client.get("presets/")).items.find(
    (preset: { name: string }) => preset.name === "Default",
  )
  const { swing_low_wave_n: _, trail_ripple_n: __, ...rest } = params
  await reset(request, {
    params: { ...rest, swing_low_h1_n: 7, trail_m15_n: 9, future_param: 42 },
  })
  await open(page)
  await expect(page.getByLabel("Swing low WAVE N")).toHaveValue("7")
  await expect(page.getByLabel("Trail RIPPLE N")).toHaveValue("9")
  await page.getByLabel("Take profit %").fill("11")
  await page.getByRole("button", { name: "Start", exact: true }).click()
  await expect(page.getByText(/^start — (accepted|pending)$/)).toBeVisible()
  await expect.poll(() => runtime().running).toBe(true)
  const sent = runtime().params
  expect(sent).toMatchObject({ swing_low_wave_n: 7, trail_ripple_n: 9 })
  for (const key of ["swing_low_h1_n", "trail_m15_n", "future_param"])
    expect(sent).not.toHaveProperty(key)
})

test("list editor against the backend: normalization, D3 and a preset made invalid", async ({
  page,
  request,
}) => {
  await reset(request)
  const client = await api(request)
  await open(page)
  await choose(page, "Preset", "Fast · built-in")
  await page.getByRole("button", { name: "Edit option lists" }).click()

  const wave = page.getByRole("region", { name: "Wave interval options" })
  const add = async (text: string) => {
    await wave.getByLabel("Add to Wave interval").fill(text)
    await wave.getByRole("button", { name: "Add", exact: true }).click()
  }
  await add("H1")
  await expect(wave.getByText("1h is already in the list.")).toBeVisible()
  await add("custom")
  await add("1M")
  await wave.getByRole("button", { name: "Save Wave interval" }).click()
  await expect(wave.getByText("Saved.")).toBeVisible()
  // The backend keeps them (D3) and reports them as unsupported.
  expect((await client.get("options/")).wave_interval).toEqual({
    options: [...baselineLists.wave_interval, "custom", "1M"],
    unsupported: ["custom", "1M"],
  })
  await page.getByLabel("Wave interval", { exact: true }).click()
  await expect(
    page.getByRole("option", { name: "1M — not supported by the agent" }),
  ).toHaveAttribute("aria-disabled", "true")
  await page.keyboard.press("Escape")

  const ripple = page.getByRole("region", { name: "Ripple interval options" })
  await ripple.getByRole("button", { name: "Remove M5" }).click()
  await expect(
    ripple.getByText(
      "Presets using a removed value will become invalid: Fast.",
    ),
  ).toBeVisible()
  await ripple.getByRole("button", { name: "Save Ripple interval" }).click()
  await expect(ripple.getByText("Saved.")).toBeVisible()
  await expect(page.getByLabel("Preset", { exact: true })).toHaveText(
    "Fast · built-in (invalid)",
  )
  await expectBlocked(page)
  // The backend agrees: Fast is no longer launchable.
  const fast = (await client.get("presets/")).items.find(
    (preset: { name: string }) => preset.name === "Fast",
  )
  expect(fast.valid).toBe(false)
  expect(fast.errors.map((error: { type: string }) => error.type)).toEqual([
    "option_value",
  ])
  // W1 and D1 stay available for the tide.
  await page.getByLabel("Tide interval", { exact: true }).click()
  for (const option of ["W1", "D1"])
    await expect(
      page.getByRole("option", { name: option, exact: true }),
    ).toBeEnabled()
})
