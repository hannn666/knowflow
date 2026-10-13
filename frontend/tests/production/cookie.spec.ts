import { test, expect } from "@playwright/test";
test("production cookie stays Secure even with legacy HTTP test override", async ({request}) => {
  const data = { email: "alice@example.com", password: "Fixture-only-password-123" };
  const rejected = await request.post("/api/auth/login", {headers:{Origin:"http://127.0.0.1:3108"},data});
  expect(rejected.status()).toBe(403);
  const result = await request.post("/api/auth/login", {headers:{Origin:"https://knowflow.example"},data});
  expect(result.status()).toBe(200);
  const cookie = result.headers()["set-cookie"];
  expect(cookie).toContain("Secure"); expect(cookie).toContain("HttpOnly"); expect(cookie).toContain("SameSite=strict"); expect(cookie).not.toContain("Domain=");
  const payload = await result.text(); expect(payload).not.toContain("access_token"); expect(payload).not.toContain("password_hash");
  expect(result.headers()["cache-control"]).toContain("no-store");
});
