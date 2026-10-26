"use client";
import { ClientError, send } from "./http-client";
import { ACTIVE_COOKIE, isSession, selectedSlot, type BrowserSession, type SessionStatus } from "./session-contracts";

const LOCK = "knowflow-auth-transition";
const EVENT = "knowflow-session-change";
const pendingKey = (slot: string) => `knowflow.auth.pending.${slot}`;
const preparing = new Map<string, Promise<BrowserSession>>();
let navigating = false;
export const activeSlot = () => selectedSlot(document.cookie);
function select(slot?: string) {
  document.cookie = `${ACTIVE_COOKIE}=${slot ?? ""}; Path=/; SameSite=Strict; Max-Age=${slot ? 28800 : 0}${location.protocol === "https:" ? "; Secure" : ""}`;
}
function changed(): never { throw new ClientError(409, "登录身份已变化，请刷新页面", "SESSION_CHANGED"); }
function assertSlot(slot: string) { if (activeSlot() !== slot) changed(); }
function notify() {
  window.dispatchEvent(new Event(EVENT));
  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel(EVENT);
    channel.postMessage("changed"); channel.close();
  }
}
async function locked<T>(work: () => Promise<T>): Promise<T> {
  if (!navigator.locks) throw new ClientError(409, "此浏览器不支持安全的多标签认证协调，请使用支持 Web Locks 的浏览器");
  try {
    return await navigator.locks.request(LOCK, { signal: AbortSignal.timeout(20000) }, work);
  } catch (e) {
    if (e instanceof ClientError) throw e;
    throw new ClientError(503, "认证操作尚未完成，请稍后重试");
  }
}
function pending(slot: string) {
  try { return localStorage.getItem(pendingKey(slot)); }
  catch { throw new ClientError(409, "浏览器无法保存认证协调状态，请启用站点存储后重新登录"); }
}
function mark(slot: string, kind?: string) {
  try {
    if (kind) localStorage.setItem(pendingKey(slot), kind);
    else localStorage.removeItem(pendingKey(slot));
  } catch { throw new ClientError(409, "浏览器无法保存认证协调状态，请启用站点存储后重新登录"); }
}
export function assertCurrent(current: BrowserSession) {
  assertSlot(current.slot);
  const kind = pending(current.slot);
  if (kind) throw pendingError(kind);
}
function pendingError(kind: string) {
  return new ClientError(409, kind === "logout" ?
    "服务端退出尚未确认，已暂停操作；请重试退出或重新登录" :
    "续期结果尚未确认，已暂停操作；请重试退出或重新登录，不会重复使用旧凭证");
}
async function status(): Promise<SessionStatus> {
  const value = await send<SessionStatus>("auth/me");
  if (isSession(value) || value && "state" in value && value.state === "refresh_required" && typeof value.slot === "string") return value;
  throw new ClientError(503, "登录服务返回无效结果");
}
function expectedSession(current: BrowserSession, expected?: BrowserSession | string) {
  if (typeof expected === "string" ? current.user.id !== expected : expected &&
      (current.slot !== expected.slot || current.sessionId !== expected.sessionId || current.user.id !== expected.user.id)) changed();
}
export async function ensureSession(expected?: BrowserSession | string, minimumMs = 60000): Promise<BrowserSession> {
  const slot = activeSlot();
  if (!slot) throw new ClientError(401, "登录会话已结束，请重新登录");
  if (expected && typeof expected !== "string" && expected.slot !== slot) changed();
  const key = `${slot}:${minimumMs}`;
  let operation = preparing.get(key);
  if (!operation) {
    operation = locked(async () => {
      assertSlot(slot);
      const kind = pending(slot);
      if (kind) throw pendingError(kind);
      let current = await status();
      assertSlot(slot);
      if (current.slot !== slot) changed();
      if (isSession(current)) expectedSession(current, expected);
      if (!isSession(current) || current.expiresAt - Date.now() < minimumMs) {
        // A durable PUBLIC marker prevents replay after timeout or page close.
        // No credential or user data is stored in localStorage.
        mark(slot, "refresh");
        try {
          const renewed = await send<BrowserSession>("auth/refresh", {}, { headers: { "X-KnowFlow-Slot": slot } });
          assertSlot(slot);
          if (!isSession(renewed) || renewed.slot !== slot) throw new ClientError(503, "登录服务返回无效结果");
          if (isSession(current) && (renewed.sessionId !== current.sessionId || renewed.user.id !== current.user.id)) changed();
          current = renewed;
          mark(slot); notify();
        } catch (e) {
          if (e instanceof ClientError && e.code === "SESSION_CHANGED") throw e;
          if (e instanceof ClientError && e.status === 401) {
            mark(slot); assertSlot(slot); select(); notify();
            throw new ClientError(401, "登录会话已失效，请重新登录");
          }
          notify();
          throw new ClientError(e instanceof ClientError ? e.status : 503,
            "续期结果尚未确认，已暂停操作；请重试退出或重新登录，不会重复使用旧凭证");
        }
      }
      if (!isSession(current)) throw new ClientError(503, "登录服务返回无效结果");
      if (current.expiresAt <= Date.now()) throw new ClientError(401, "登录凭证已过期，请重新登录");
      return current;
    });
    preparing.set(key, operation);
    void operation.finally(() => { if (preparing.get(key) === operation) preparing.delete(key); }).catch(() => {});
  }
  const current = await operation;
  assertSlot(slot); expectedSession(current, expected);
  return current;
}
export async function confirmResultSession(expected: BrowserSession): Promise<BrowserSession> {
  // Restore credentials if necessary, but never resend the business request.
  try { await ensureSession(expected); }
  catch (error) { assertCurrent(expected); throw error; }
  return locked(async () => {
    assertCurrent(expected);
    // Do not reuse a preparation promise that could predate result completion.
    let current: SessionStatus;
    try { current = await status(); }
    catch (error) { assertCurrent(expected); throw error; }
    assertCurrent(expected);
    if (!isSession(current)) throw new ClientError(401, "登录会话已失效，未显示问答结果");
    expectedSession(current, expected);
    return current;
  });
}
export function watchResultInvalidation(clear: () => void) {
  // Hide already displayed evidence on auth changes or lifecycle transitions.
  // An in-flight answer still needs a fresh authoritative check before display.
  const channel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(EVENT) : undefined;
  if (channel) channel.onmessage = clear;
  const storage = (event: StorageEvent) => {
    if (event.key === null || event.key.startsWith("knowflow.auth.pending.")) clear();
  };
  window.addEventListener(EVENT, clear);
  window.addEventListener("storage", storage);
  window.addEventListener("pagehide", clear);
  window.addEventListener("pageshow", clear);
  window.addEventListener("focus", clear);
  document.addEventListener("visibilitychange", clear);
  return () => {
    channel?.close(); window.removeEventListener(EVENT, clear);
    window.removeEventListener("storage", storage);
    window.removeEventListener("pagehide", clear); window.removeEventListener("pageshow", clear);
    window.removeEventListener("focus", clear); document.removeEventListener("visibilitychange", clear);
  };
}
export async function login(data: unknown) {
  return locked(async () => {
    const previous = activeSlot();
    const slot = crypto.randomUUID();
    mark(slot, "login"); // Check storage BEFORE issuing credentials.
    try {
      const current = await send<BrowserSession>("auth/login", data, { headers: { "X-KnowFlow-Slot": slot } });
      if (!isSession(current) || current.slot !== slot) throw new ClientError(503, "登录服务返回无效结果");
      if (activeSlot() !== previous) changed();
      select(slot); mark(slot);
      if (previous) mark(previous);
      notify(); return current;
    } catch (e) { mark(slot); throw e; }
  });
}
export async function logout(expected?: BrowserSession) {
  return locked(async () => {
    const slot = activeSlot();
    if (!slot) throw new ClientError(401, "会话凭证缺失，无法确认服务端退出");
    if (expected && expected.slot !== slot) changed();
    mark(slot, "logout"); notify();
    // Explicit retries are safe: backend logout also accepts consumed RTs.
    await send("auth/logout", {}, { headers: { "X-KnowFlow-Slot": slot } });
    assertSlot(slot); select(); mark(slot); notify();
    return { ok: true };
  });
}
export function sessionNavigation(error: unknown) {
  const target = error instanceof ClientError && error.code === "SESSION_CHANGED" ? "/knowledge-bases" :
    error instanceof ClientError && error.status === 401 ? "/login?expired=1" : undefined;
  if (target && !navigating) { navigating = true; window.location.replace(target); }
}
export function watchSession(initial: BrowserSession, failure: (error: unknown) => void) {
  let stopped = false;
  let checking = false;
  let timer: ReturnType<typeof setTimeout>;
  const check = async () => {
    if (checking || stopped) return;
    clearTimeout(timer);
    if (document.visibilityState !== "visible") return;
    checking = true;
    try {
      const current = await ensureSession(initial);
      if (!stopped) timer = setTimeout(() => void check(), Math.max(1000, current.expiresAt - Date.now() - 60000));
    } catch (e) { if (!stopped) { failure(e); sessionNavigation(e); } }
    finally { checking = false; }
  };
  const changed = () => { if (!stopped) void check(); };
  const channel = typeof BroadcastChannel !== "undefined" ? new BroadcastChannel(EVENT) : undefined;
  if (channel) channel.onmessage = changed;
  window.addEventListener(EVENT, changed);
  window.addEventListener("pageshow", changed);
  window.addEventListener("focus", changed);
  document.addEventListener("visibilitychange", changed);
  void check();
  return () => {
    stopped = true; clearTimeout(timer); channel?.close();
    window.removeEventListener(EVENT, changed); window.removeEventListener("pageshow", changed);
    window.removeEventListener("focus", changed); document.removeEventListener("visibilitychange", changed);
  };
}
