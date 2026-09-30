// UI runner of the shared validation fixture (plan §6, PRESET-07). Every case
// whose `ui` outcome is not "n/a" is typed into the Strategy form on top of
// the baseline params; an `ok` case must send exactly the baseline (+ const)
// with `expected_sent` applied, an `error` case must keep Start off and show
// exactly the expected set of (category, field) pairs.
import { readFileSync } from "node:fs"
import { expect, type Page, test } from "@playwright/test"
import type { TradingStatus } from "../src/client"
import strategyCatalog from "./fixtures/strategy-catalog.json"
import { accepted, expectBlocked, mock, snapshot } from "./utils/tradingMock"

type Outcome =
  | "n/a"
  | { result: "ok" }
  | {
      result: "error"
      errors: { category: string; field: string | null }[]
    }
type Case = {
  id: string
  ui_draft: Record<string, string> | null
  expected_sent: Record<string, unknown> | null
  outcomes: Record<string, Outcome>
}
type Fixture = {
  baseline: {
    options: Record<string, string[]>
    params: Record<string, unknown>
    network: "testnet" | "live"
  }
  error_map: { ui: { types: Record<string, string> } }
  stages: Record<string, string>
  cases: Case[]
}

const FIXTURE: Fixture = JSON.parse(
  readFileSync(
    new URL("../../tests/fixtures/validation-edge-cases.json", import.meta.url),
    "utf8",
  ),
)
const PROPERTIES: Record<
  string,
  { title: string; enum?: unknown[]; type?: string; const?: unknown }
> = strategyCatalog.strategies[0].settings_schema.properties
const CONSTS = Object.fromEntries(
  Object.entries(PROPERTIES)
    .filter(([, prop]) => prop.const !== undefined)
    .map(([key, prop]) => [key, prop.const]),
)

function baselineStatus(): TradingStatus {
  const state = snapshot()
  state.desired = {
    ...state.desired,
    running: false,
    network: FIXTURE.baseline.network,
    params: structuredClone(FIXTURE.baseline.params),
  }
  state.actual!.state = "idle"
  return state
}

/**
 * Split the draft into what is typed and what the form cannot type: a
 * `Select` value and a list longer than its inputs arrive as the saved
 * desired params instead, which the form checks the same way (§4.5.3).
 */
function plan(draft: Record<string, string>) {
  const typed: [string, string][] = []
  const seeded: Record<string, unknown> = {}
  for (const [key, text] of Object.entries(draft)) {
    const prop = PROPERTIES[key]
    if (!prop) throw new Error(`fixture names an unknown field ${key}`)
    if (Array.isArray(prop.enum)) seeded[key] = text
    else if (prop.type === "array") {
      const parts = text.split(",").map((part) => part.trim())
      if (parts.length > 3) seeded[key] = parts
      else
        parts
          .concat(Array(3 - parts.length).fill(""))
          .forEach((part, index) => {
            typed.push([`${prop.title} #${index + 1}`, part])
          })
    } else typed.push([prop.title, text])
  }
  return { typed, seeded }
}

async function shownIssues(page: Page): Promise<string[]> {
  const issues = page.locator("[data-issue-type]")
  const found: string[] = []
  for (const issue of await issues.all()) {
    const type = (await issue.getAttribute("data-issue-type")) ?? ""
    const field = (await issue.getAttribute("data-issue-field")) || null
    const category = FIXTURE.error_map.ui.types[type]
    // An unknown UI error type fails the runner, it is never skipped.
    expect(category, `error_map.ui has no entry for ${type}`).toBeDefined()
    found.push(`${category}|${field}`)
  }
  return [...new Set(found)].sort()
}

for (const fixtureCase of FIXTURE.cases) {
  const outcome = fixtureCase.outcomes.ui
  // An absent key is a fixture error, not a skip (§6).
  if (outcome === undefined)
    throw new Error(`${fixtureCase.id}: outcomes.ui is missing`)

  test(`fixture ${fixtureCase.id}`, async ({ page }) => {
    test.skip(
      FIXTURE.stages.ui !== undefined,
      `deferred: ui runner belongs to ${FIXTURE.stages.ui}`,
    )
    test.skip(outcome === "n/a", "n/a: the UI cannot express this input")
    if (outcome === "n/a" || fixtureCase.ui_draft === null)
      throw new Error(`${fixtureCase.id}: a ui outcome needs a ui_draft`)

    const { typed, seeded } = plan(fixtureCase.ui_draft)
    const status = baselineStatus()
    status.desired.params = { ...status.desired.params, ...seeded }
    const api = await mock(page, status)
    api.presets.options = structuredClone(FIXTURE.baseline.options)
    await page.goto("/strategy")
    await expect(page.getByLabel("Kline window", { exact: true })).toHaveValue(
      String(FIXTURE.baseline.params.kline_window),
    )
    for (const [label, text] of typed)
      await page.getByLabel(label, { exact: true }).fill(text)

    const start = page.getByRole("button", { name: "Start", exact: true })
    if (outcome.result === "ok") {
      await start.click()
      await expect(page.getByText(accepted("start"))).toBeVisible()
      const body = api.posts[0].body as { params: Record<string, unknown> }
      expect(body.params).toEqual({
        ...FIXTURE.baseline.params,
        ...CONSTS,
        ...fixtureCase.expected_sent,
      })
      return
    }
    await expectBlocked(page)
    expect(await shownIssues(page)).toEqual(
      [
        ...new Set(
          outcome.errors.map((error) => `${error.category}|${error.field}`),
        ),
      ].sort(),
    )
    expect(api.posts).toEqual([])
  })
}
