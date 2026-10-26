import { ClientError, send } from "./http-client";
import { activeSlot, assertCurrent, ensureSession, login, logout, sessionNavigation } from "./session-client";
import type { BrowserSession } from "./session-contracts";
export { ClientError } from "./http-client";
export async function api<T>(path: string, data?: unknown, expected?: BrowserSession | string,
  options?: { file?: File; noBody?: boolean; timeoutMs?: number }): Promise<T> {
  if (path === "auth/login") return await login(data) as T;
  if (path === "auth/logout") return await logout(typeof expected === "object" ? expected : undefined) as T;
  let snapshot: BrowserSession;
  try { snapshot = await ensureSession(expected, path.endsWith("/ask") ? 360000 : 60000); }
  catch (e) { sessionNavigation(e); throw e; }
  if (path === "auth/me") return snapshot as T;
  // Exactly one business attempt. A 401 or uncertain POST never replays it.
  try {
    const result = await send<T>(path, data, { ...options, headers: {
      "X-KnowFlow-User": snapshot.user.id, "X-KnowFlow-Session": snapshot.sessionId,
      "X-KnowFlow-Slot": snapshot.slot,
    } });
    // A legitimate rotation may still hold the cross-tab auth lock when a
    // long answer arrives. Wait for it; uncertain/logout transitions still fail
    // closed. This prepares credentials only and never repeats the ask POST.
    if (path.endsWith("/ask")) await ensureSession(snapshot, 0);
    else assertCurrent(snapshot);
    return result;
  } catch (e) {
    // An old response must not invalidate a new login, including the same user.
    if (activeSlot() !== snapshot.slot) {
      const changed = new ClientError(409, "登录身份已变化，请刷新页面", "SESSION_CHANGED");
      sessionNavigation(changed); throw changed;
    }
    if (e instanceof ClientError && e.status === 401) {
      try { await ensureSession(snapshot); } catch (authError) { sessionNavigation(authError); }
      throw new ClientError(401, "本次操作未完成，请确认当前状态后手动重试；没有自动重发请求");
    }
    throw e;
  }
}
