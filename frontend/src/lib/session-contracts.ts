import { isUser, uuid, type User } from "./contracts";

// This selector is public metadata, never an authorization credential. Only the
// selected slot's backend-validated HttpOnly credentials can authorize a request.
export const ACTIVE_COOKIE = "knowflow_active";
export const accessCookie = (slot: string) => `knowflow_access_${slot}`;
export const refreshCookie = (slot: string) => `knowflow_refresh_${slot}`;
export type BrowserSession = { user: User; slot: string; sessionId: string; expiresAt: number };
export type SessionStatus = BrowserSession | { state: "refresh_required"; slot: string };
export function selectedSlot(header: string) {
  const value = header.split(";").map(v => v.trim()).find(v => v.startsWith(`${ACTIVE_COOKIE}=`))?.slice(ACTIVE_COOKIE.length + 1);
  return value && uuid.test(value) ? value : undefined;
}
export function isSession(value: unknown): value is BrowserSession {
  if (!value || typeof value !== "object") return false;
  const s = value as BrowserSession;
  return isUser(s.user) && typeof s.slot === "string" && uuid.test(s.slot) &&
    typeof s.sessionId === "string" && uuid.test(s.sessionId) &&
    typeof s.expiresAt === "number" && Number.isFinite(s.expiresAt);
}
export function safeReturn(value: unknown) {
  return typeof value === "string" && (value === "/knowledge-bases" ||
    /^\/knowledge-bases\/[0-9a-f-]{36}$/i.test(value) && uuid.test(value.slice(17)))
    ? value : "/knowledge-bases";
}
