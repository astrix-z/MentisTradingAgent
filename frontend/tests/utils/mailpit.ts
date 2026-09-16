import type { APIRequestContext } from "@playwright/test"

type EmailSummary = {
  ID: string
}

/** What to do about a missing mailpit, appended to every diagnostic here. */
const PREREQUISITE =
  "Mailpit is an optional Compose service, so a plain `docker compose up -d` " +
  "leaves it down and this test cannot read its email. Start it with " +
  "`docker compose --profile extras up -d --wait db redis mailpit`, and make " +
  "sure the backend can send at all: it needs EMAILS_FROM_EMAIL and SMTP_HOST " +
  "(compose.override.yml sets both for local runs)."

/**
 * Tests that read a password-reset link out of an email need mailpit. When it
 * is not running they have to skip with that reason, rather than fail with a
 * bare ECONNREFUSED that reads like a product regression (TASKS.md T05).
 *
 * Only an unreachable host counts as unavailable: any response at all means the
 * service is up, so a wrong probe path or an authentication problem still
 * surfaces as a real failure instead of a silent skip.
 */
export async function mailpitUnavailableReason(
  request: APIRequestContext,
): Promise<string | null> {
  const host = process.env.MAILPIT_HOST

  if (!host) {
    return `MAILPIT_HOST is not set. ${PREREQUISITE}`
  }

  try {
    await request.get(`${host}/api/v1/messages`, { timeout: 2000 })
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)
    return `mailpit is not reachable at ${host} (${detail}). ${PREREQUISITE}`
  }

  return null
}

export async function waitForEmailHtml({
  request,
  query,
  timeout = 5000,
}: {
  request: APIRequestContext
  query: string
  timeout?: number
}) {
  const deadline = Date.now() + timeout

  while (Date.now() < deadline) {
    const response = await request.get(
      `${process.env.MAILPIT_HOST}/api/v1/search`,
      {
        params: { query, limit: 1 },
      },
    )
    const { messages }: { messages: EmailSummary[] } = await response.json()
    const email = messages[0]

    if (email) {
      const htmlResponse = await request.get(
        `${process.env.MAILPIT_HOST}/view/${email.ID}.html`,
      )

      if (!htmlResponse.ok()) {
        throw new Error(
          `Could not get the HTML for email "${email.ID}": ${htmlResponse.status()}`,
        )
      }

      return htmlResponse.text()
    }

    await new Promise((resolve) => setTimeout(resolve, 100))
  }

  // Mailpit being up is not enough: the backend asserts on a non-empty
  // EMAILS_FROM_EMAIL before it talks to SMTP, so an empty from-address looks
  // exactly like a slow email. Say both reasons rather than only the timeout.
  throw new Error(
    `No email matching "${query}" arrived within ${timeout}ms. Either the ` +
      `backend never sent one — it needs EMAILS_FROM_EMAIL and SMTP_HOST, and ` +
      `asserts on a missing from-address rather than logging — or mailpit received ` +
      `it under a different query. ${PREREQUISITE}`,
  )
}
