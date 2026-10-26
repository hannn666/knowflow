import { apiLogin } from "./auth-helpers";
import { test, expect } from "@playwright/test";
const credentials = {
  email: "alice@example.com",
  password: "Fixture-only-password-123",
};
const write = {
  Origin: "http://127.0.0.1:3107",
  "Content-Type": "application/json",
};
async function login(
  page: import("@playwright/test").Page,
  email = credentials.email,
) {
  await page.goto("/login");
  await page.getByLabel("邮箱", { exact: true }).fill(email);
  await page.getByLabel("密码", { exact: true }).fill(credentials.password);
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page).toHaveURL(/knowledge-bases$/);
  await expect(page.getByRole("heading", { name: "我的知识库" })).toBeVisible();
}
test("private redirect, login failure and no credential echo", async ({
  page,
}) => {
  await page.goto("/knowledge-bases");
  await expect(page).toHaveURL(/login/);
  await page.getByLabel("邮箱", { exact: true }).fill(credentials.email);
  await page.getByLabel("密码", { exact: true }).fill("wrong-password");
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page.locator(".error[role=alert]")).toContainText(
    "邮箱或密码错误",
  );
  await expect(page.locator(".error[role=alert]")).not.toContainText(
    "wrong-password",
  );
});
test("real list/create/detail, refresh, cookies, logout and A/B isolation", async ({
  page,
  context,
}) => {
  await login(page);
  const name = "学习空间-" + Date.now();
  await page.getByLabel("知识库名称").fill(name);
  await page.getByRole("button", { name: "创建知识库" }).click();
  const card = page
    .getByRole("button")
    .filter({ has: page.getByRole("heading", { name, exact: true }) });
  await expect(card).toBeVisible();
  await card.click();
  const detail = page.getByRole("region", { name: "知识库信息" });
  await expect(detail).toContainText(name);
  const id = (await detail.innerText()).match(
    /[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/,
  )![0];
  const session = await context.request.get("/api/auth/me");
  expect(session.headers()["cache-control"]).toContain("no-store");
  expect(await session.text()).not.toContain("access_token");
  expect(await session.text()).not.toContain("password_hash");
  const cookie = (await context.cookies()).find(
    (c) => c.name.startsWith("knowflow_access_"),
  )!;
  expect(cookie.httpOnly).toBeTruthy();
  expect(cookie.sameSite).toBe("Strict");
  expect(cookie.expires - Date.now() / 1000).toBeLessThanOrEqual(900);
  expect(await page.evaluate(() => document.cookie)).not.toContain(
    "knowflow_session",
  );
  expect(await page.evaluate(() => Object.keys(localStorage))).toEqual([]);
  expect(await page.content()).not.toContain(cookie.value);
  await page.reload();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: `test-results/${test.info().project.name}-${process.env.M51_REAL === "1" ? "real" : "mock"}-workspace.png`,
    fullPage: true,
  });
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page).toHaveURL(/login/);
  expect(
    (await context.cookies()).some((c) => c.name.startsWith("knowflow_access_")),
  ).toBeFalsy();
  await login(page, "bob@example.com");
  await expect(page.getByRole("heading", { name, exact: true })).toHaveCount(0);
  const foreign = await context.request.get(`/api/knowledge-bases/${id}`);
  expect(foreign.status()).toBe(404);
  expect(await foreign.text()).not.toContain(name);
});
test("CSRF, allowlist, validation and actual body cap", async ({ context }) => {
  const request = context.request;
  for (const origin of ["https://attacker.example", undefined]) {
    const r = await request.post("/api/auth/login", {
      headers: origin ? { Origin: origin } : {},
      data: credentials,
    });
    expect(r.status()).toBe(403);
  }
  const r = await request.post("/api/auth/login", {
    headers: write,
    data: { ...credentials, owner_id: "private-sentinel" },
  });
  expect(r.status()).toBe(422);
  expect(await r.text()).not.toContain("sentinel");
  const huge = await request.post("/api/auth/login", {
    headers: write,
    data: { email: credentials.email, password: "x".repeat(5000) },
  });
  expect(huge.status()).toBe(413);
  expect((await request.get("/api/http://attacker.example")).status()).toBe(
    404,
  );
  const login = await apiLogin(context);
  expect(login.status()).toBe(200);
  expect(await login.text()).not.toContain("access_token");
  expect(login.headers()["set-cookie"]).toContain("HttpOnly");
  const denied = await request.post("/api/knowledge-bases", {
    headers: { ...write, Origin: "https://attacker.example" },
    data: { name: "跨站请求" },
  });
  expect(denied.status()).toBe(403);
});
test("expired Access is recovered by refresh without a forced logout", async ({ page, context }) => {
  const result = await apiLogin(context, "short@example.com");
  expect(result.status()).toBe(200);
  const before = await result.json();
  await page.goto("/knowledge-bases");
  await expect(page).toHaveURL(/knowledge-bases$/);
  await expect.poll(async () => (await (await context.request.get("/api/auth/me")).json()).expiresAt).toBeGreaterThan(before.expiresAt);
  await page.reload();
  await expect(page).toHaveURL(/knowledge-bases$/);
});
test("mock backend errors never expose internal details", async ({ context }) => {
  const request = context.request;
  test.skip(
    process.env.M51_REAL === "1",
    "Controlled fault injection only applies to mock backend",
  );
  await apiLogin(context);
  for (const status of [403, 404, 409, 422, 503]) {
    const r = await request.post("/api/knowledge-bases", {
      headers: write,
      data: { name: `故障${status}` },
    });
    expect(r.status()).toBe(status);
    expect(await r.text()).not.toContain("sentinel");
    expect(r.headers()["cache-control"]).toContain("no-store");
  }
});

test("stale tab cannot mutate a newly logged-in account", async ({ context }) => {
  const request = context.request;
  const first = await apiLogin(context);
  const user = (await first.json()).user;
  await apiLogin(context, "bob@example.com");
  const name = "不得跨会话创建-" + Date.now();
  const response = await request.post("/api/knowledge-bases", {
    headers: { ...write, "X-KnowFlow-User": user.id },
    data: { name },
  });
  expect(response.status()).toBe(409);
  expect(
    await (await request.get("/api/knowledge-bases")).text(),
  ).not.toContain(name);
  const csrf = await request.post("/api/auth/logout", {
    headers: { ...write, "Sec-Fetch-Site": "cross-site" },
    data: {},
  });
  expect(csrf.status()).toBe(403);
});

test("loading, duplicate submission and network error are visible", async ({
  page,
}) => {
  await login(page);
  let writes = 0;
  await page.route("**/api/knowledge-bases", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    writes++;
    const response = await route.fetch();
    await new Promise((resolve) => setTimeout(resolve, 400));
    await route.fulfill({ response });
  });
  const name = "重复提交保护-" + Date.now();
  await page.getByLabel("知识库名称").fill(name);
  await page.getByRole("button", { name: "创建知识库" }).click();
  await page
    .locator(".create-panel form")
    .evaluate((form) => (form as HTMLFormElement).requestSubmit());
  await expect(page.getByRole("button", { name: "正在创建" })).toBeDisabled();
  await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
  expect(writes).toBe(1);
  await page.unroute("**/api/knowledge-bases");
  await page.route("**/api/knowledge-bases?*", (route) => route.abort());
  await page.getByRole("button", { name: "刷新列表" }).click();
  await expect(page.locator(".error[role=alert]")).toContainText(
    "网络连接失败",
  );
  await page.unroute("**/api/knowledge-bases?*");
  await page.route("**/api/knowledge-bases?*", (route) =>
    route.fulfill({
      status: 502,
      contentType: "text/html",
      body: "private-path-and-database-sentinel",
    }),
  );
  await page.getByRole("button", { name: "刷新列表" }).click();
  await expect(page.locator(".error[role=alert]")).toContainText(
    "服务返回异常",
  );
  await expect(page.locator(".error[role=alert]")).not.toContainText(
    "sentinel",
  );
});
