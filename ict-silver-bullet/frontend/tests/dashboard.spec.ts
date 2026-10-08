import { expect, test, type Page } from "@playwright/test";
async function signIn(page: Page) {
  await page.goto("/");
  await page.getByLabel("Username").fill("fixture-user");
  await page.getByLabel("Password", { exact: true }).fill("fixture-password");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(
    page.getByRole("heading", { name: "Trading overview." }),
  ).toBeVisible();
}
test("production charts render under CSP; trades filter and logout revokes access", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => {
    if (
      message.type() === "error" &&
      /Content Security Policy|Refused to/.test(message.text())
    )
      errors.push(message.text());
  });
  await signIn(page);
  await expect(page.locator("#equity .scatterlayer")).toBeVisible();
  await expect(page.locator("#setup .boxlayer .trace")).toBeVisible();
  await page.getByLabel("Filter trades").fill("GBPUSD");
  await expect(page.locator("#trades tbody tr")).toHaveCount(3);
  await expect(
    page.getByRole("button", { name: "Share chart..." }),
  ).toHaveCount(0);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/dashboard-desktop.png",
    fullPage: true,
  });
  expect(errors).toEqual([]);
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByLabel("Username")).toBeVisible();
  expect((await page.request.get("/api/v1/dashboard")).status()).toBe(401);
});
test("mobile has no page overflow and keeps charts and filters usable", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await expect(page.locator("#setup .boxlayer .trace")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/dashboard-mobile.png",
    fullPage: true,
  });
});
test("partial failure and empty positions are explicit", async ({ page }) => {
  await page.route("**/api/v1/dashboard", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.news = {
      status: "unavailable",
      data: null,
      updated_at: null,
      message: "Read failed; retrying",
    };
    data.positions = {
      status: "empty",
      data: [],
      updated_at: data.generated_at,
      message: "No data yet",
    };
    data.equity.status = "stale";
    data.equity.message = "Read failed; retrying";
    await route.fulfill({ response, json: data });
  });
  await signIn(page);
  await expect(
    page.getByText("No open positions", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Calendar unavailable", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("#equity .panel-warning")).toHaveText(
    "Read failed; retrying",
  );
});
test("source age advances offline and blackout stops claiming clear", async ({
  page,
}) => {
  await page.route("**/api/v1/dashboard", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.news.updated_at = new Date(
      Date.parse(data.generated_at) - 35000,
    ).toISOString();
    data.news.data.blackout.forEach(
      (b: { blocked: boolean }) => (b.blocked = false),
    );
    await route.fulfill({ response, json: data });
  });
  await signIn(page);
  await expect(
    page.getByText("EURUSD · unknown", { exact: true }),
  ).toBeVisible();
  const badge = page
    .locator(".card")
    .filter({ has: page.getByRole("heading", { name: "Economic calendar" }) })
    .locator(".panel-state");
  const before = await badge.textContent();
  await page.waitForTimeout(1500);
  expect(await badge.textContent()).not.toBe(before);
});
test("expired session returns to login and clears the workspace", async ({
  page,
}) => {
  await page.route("**/api/v1/dashboard", (route) =>
    route.fulfill({ status: 401, json: { detail: "Session required" } }),
  );
  await page.goto("/");
  await page.getByLabel("Username").fill("fixture-user");
  await page.getByLabel("Password", { exact: true }).fill("fixture-password");
  await page.getByRole("button", { name: "Sign in →" }).click();
  await expect(page.getByRole("alert")).toHaveText(
    "Your session has expired. Sign in again.",
  );
  await expect(page.getByLabel("Username")).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Trading overview." }),
  ).toHaveCount(0);
});

test("equity zoom survives the next ten-second poll", async ({ page }) => {
  await signIn(page);
  await expect(page.locator("#equity .scatterlayer")).toBeVisible();
  const ticks = page.locator("#equity .xaxislayer-above .xtick text");
  const labels = async () => (await ticks.allTextContents()).sort();
  const initial = await labels();
  await page.locator("#equity").hover();
  await page
    .locator("#equity")
    .getByRole("button", { name: "Zoom in", exact: true })
    .click();
  await expect.poll(labels).not.toEqual(initial);
  const zoomed = await labels();
  await page.waitForResponse((r) => r.url().endsWith("/api/v1/dashboard"), {
    timeout: 15000,
  });
  await expect.poll(labels).toEqual(zoomed);
});

test("poll failure keeps last data and marks risk and blackout uncertain", async ({
  page,
}) => {
  let calls = 0;
  await page.route("**/api/v1/dashboard", (route) =>
    ++calls === 1
      ? route.continue()
      : route.fulfill({ status: 503, json: { detail: "offline" } }),
  );
  await signIn(page);
  await expect(page.getByText("$24,250.00", { exact: true })).toBeVisible();
  await page.waitForResponse(
    (r) => r.url().endsWith("/api/v1/dashboard") && r.status() === 503,
    { timeout: 15000 },
  );
  await expect(page.getByRole("alert")).toContainText(
    "Showing the last received snapshot.",
  );
  await expect(page.getByText("$24,250.00", { exact: true })).toBeVisible();
  await expect(
    page.getByText("EURUSD · unknown", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Last reported", { exact: true })).toHaveCount(2);
});
