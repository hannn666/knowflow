import { randomUUID } from "node:crypto";
import type { BrowserContext } from "@playwright/test";
export const origin = "http://127.0.0.1:3107";
export const credentials = { email: "alice@example.com", password: "Fixture-only-password-123" };
// API tests explicitly perform the same public selector commit as the browser.
// The BFF must NEVER set this pointer in a potentially delayed HTTP response.
export async function apiLogin(context: BrowserContext, email = credentials.email) {
  const slot = randomUUID();
  const response = await context.request.post("/api/auth/login", {
    headers: { Origin: origin, "X-KnowFlow-Slot": slot }, data: { ...credentials, email },
  });
  if (response.ok()) await context.addCookies([{ name: "knowflow_active", value: slot,
    url: origin, sameSite: "Strict", httpOnly: false }]);
  return response;
}
