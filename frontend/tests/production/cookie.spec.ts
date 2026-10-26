import { randomUUID } from "node:crypto";
import { test, expect } from "@playwright/test";
test("production cookie stays Secure even with legacy HTTP test override", async ({request}) => {
  const data = { email: "alice@example.com", password: "Fixture-only-password-123" };
  const rejected = await request.post("/api/auth/login", {headers:{Origin:"http://127.0.0.1:3108"},data});
  expect(rejected.status()).toBe(403);
  const slot = randomUUID();
  const result = await request.post("/api/auth/login", {headers:{Origin:"https://knowflow.example", "X-KnowFlow-Slot": slot},data});
  expect(result.status()).toBe(200);
  const cookie = result.headers()["set-cookie"];
  expect(cookie).toContain("Secure"); expect(cookie).toContain("HttpOnly"); expect(cookie).toContain("SameSite=strict"); expect(cookie).not.toContain("Domain=");
  const payload = await result.text(); expect(payload).not.toContain("access_token"); expect(payload).not.toContain("password_hash");
  expect(result.headers()["cache-control"]).toContain("no-store");
  const pairs = result.headersArray().filter(h=>h.name.toLowerCase()==="set-cookie").map(h=>h.value);
  expect(pairs).toHaveLength(2);
  for(const value of pairs) {
    expect(value).toContain("Secure"); expect(value).toContain("HttpOnly");
    expect(value).toContain("SameSite=strict"); expect(value).not.toContain("Domain=");
  }
  expect(pairs.find(v=>v.startsWith("knowflow_refresh_"))).toContain("Path=/api/auth");
  expect(cookie).not.toContain("knowflow_active=");
  const cookieHeader = [`knowflow_active=${slot}`,...pairs.map(v=>v.split(";")[0])].join("; ");
  const logout = await request.post("/api/auth/logout", {
    headers:{ Origin:"https://knowflow.example", Cookie:cookieHeader, "X-KnowFlow-Slot":slot }, data:{},
  });
  expect(logout.status()).toBe(200);
  const cleared = logout.headersArray().filter(h=>h.name.toLowerCase()==="set-cookie").map(h=>h.value);
  for(const prefix of ["knowflow_access_","knowflow_refresh_"]) {
    const value=cleared.find(v=>v.startsWith(prefix))!;
    expect(value).toContain("Max-Age=0"); expect(value).toContain("Secure"); expect(value).toContain("HttpOnly");
  }
  expect(cleared.find(v=>v.startsWith("knowflow_refresh_"))).toContain("Path=/api/auth");
});
