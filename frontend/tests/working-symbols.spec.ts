import { expect, test } from "@playwright/test"

/**
 * The only live test of /working-symbols. The page is covered in detail by the
 * mock suite in trading-contract.spec.ts, but none of that can tell us whether the
 * real backend and the real agent agree well enough for the page to render.
 *
 * It is written against state rather than against one expected answer, because the
 * agent may simply not have published a universe yet — in CI it often will not.
 * So it accepts the table or the page's own explanation that there is nothing to
 * show, and fails only if the page falls over or claims it cannot reach the agent.
 * That is the regression worth catching here (TASKS.md T06).
 */
test("working symbols renders a sortable table, or says why it has none", async ({
  page,
}) => {
  await page.goto("/working-symbols")

  await expect(
    page.getByRole("heading", { name: "Working Symbols" }),
  ).toBeVisible()

  const unpublished = page.getByText(/has not published a symbol list yet/)
  const volumeHeader = page.getByRole("button", { name: "24h volume (USDT)" })

  // Either answer is fine; silence is not.
  await expect(unpublished.or(volumeHeader).first()).toBeVisible()

  // And an unreachable agent is never fine, whichever branch we are in.
  await expect(
    page.getByText(/Cannot load the symbol list from the agent/),
  ).toHaveCount(0)

  if (await unpublished.isVisible()) {
    await expect(page.locator("tbody tr")).toHaveCount(0)
    return
  }

  // Published: the page promises a table that sorts on every column. The volume
  // cell carries the raw value in its title, so the ordering can be checked
  // numerically instead of by reading formatted text back.
  const volumes = () =>
    page
      .locator("tbody tr td:nth-child(4) span")
      .evaluateAll((cells) =>
        cells.map((cell) => Number(cell.getAttribute("title") ?? 0)),
      )

  const rows = await page.locator("tbody tr").count()
  expect(rows).toBeGreaterThan(0)

  await volumeHeader.click()
  const ascending = await volumes()
  expect(ascending).toEqual([...ascending].sort((a, b) => a - b))

  // Clicking the same header again reverses the order rather than clearing it.
  await volumeHeader.click()
  const descending = await volumes()
  expect(descending).toEqual([...descending].sort((a, b) => b - a))
  expect(descending).toHaveLength(rows)
})
