import "server-only";
import { cookies } from "next/headers";
import { type NextResponse } from "next/server";
import { uuid } from "./contracts";
import { ACTIVE_COOKIE, accessCookie, refreshCookie } from "./session-contracts";
import { body, checkWrite, error, expiry, json, me, SafeError, session, sessionId, upstream } from "./backend";

const flags = { httpOnly: true, sameSite: "strict" as const, secure: process.env.NODE_ENV === "production" };
function clearSlot(response: NextResponse, slot: string) {
  response.cookies.set(accessCookie(slot), "", { ...flags, path: "/", maxAge: 0 });
  response.cookies.set(refreshCookie(slot), "", { ...flags, path: "/api/auth", maxAge: 0 });
  // Remove the pre-renewal Access cookie during an explicit auth transition.
  response.cookies.set("knowflow_session", "", { ...flags, path: "/", maxAge: 0 });
  return response;
}
async function issued(raw: unknown, slot: string) {
  if (!raw || typeof raw !== "object") throw new SafeError(503, "登录服务返回无效结果");
  const r = raw as Record<string, unknown>;
  const absolute = typeof r.session_expires_at === "string" ? Date.parse(r.session_expires_at) : NaN;
  const idle = typeof r.idle_expires_at === "string" ? Date.parse(r.idle_expires_at) : NaN;
  if (typeof r.access_token !== "string" || r.access_token.length > 2048 ||
      !/^[A-Za-z0-9_.-]+$/.test(r.access_token) || typeof r.refresh_token !== "string" ||
      !/^[A-Za-z0-9_-]{43}$/.test(r.refresh_token) || r.token_type !== "bearer" ||
      typeof r.expires_in !== "number" || !Number.isInteger(r.expires_in) || r.expires_in <= 0 || r.expires_in > 900 ||
      typeof r.session_id !== "string" || !uuid.test(r.session_id) ||
      !Number.isFinite(absolute) || !Number.isFinite(idle) || absolute <= Date.now() ||
      absolute > Date.now() + 28800000 + 5000 || idle > absolute || idle <= Date.now())
    throw new SafeError(503, "登录服务返回无效结果");
  const user = await me(r.access_token);
  if (sessionId(r.access_token) !== r.session_id) throw new SafeError(503, "登录服务返回无效结果");
  const expiresAt = expiry(r.access_token);
  const age = Math.floor(Math.min(r.expires_in, (expiresAt - Date.now()) / 1000));
  if (age <= 0) throw new SafeError(401, "登录凭证已过期，请重新登录");
  const response = json({ user, slot, sessionId: r.session_id, expiresAt });
  response.cookies.set(accessCookie(slot), r.access_token, { ...flags, path: "/", maxAge: age });
  // Do not cap at the old idle deadline: business admission can extend idle.
  // Refresh itself cannot extend either server-enforced deadline.
  response.cookies.set(refreshCookie(slot), r.refresh_token, {
    ...flags, path: "/api/auth", maxAge: Math.min(28800, Math.floor((absolute - Date.now()) / 1000)),
  });
  return response;
}
export async function authRoute(request: Request, path: string) {
  const jar = await cookies();
  const slot = jar.get(ACTIVE_COOKIE)?.value;
  const current = slot && uuid.test(slot) ? slot : undefined;
  const refresh = current ? jar.get(refreshCookie(current))?.value : undefined;
  if (new URL(request.url).search) throw new SafeError(422, "认证接口不接受查询参数");
  if (path === "auth/me" && request.method === "GET") {
    try { return json(await session()); }
    catch (e) {
      if (e instanceof SafeError && e.status === 401 && current && refresh)
        return json({ state: "refresh_required", slot: current });
      throw e;
    }
  }
  if (request.method !== "POST" || !["auth/login", "auth/refresh", "auth/logout"].includes(path))
    throw new SafeError(404, "接口不存在");
  checkWrite(request);
  const data = await body(request);
  if (path === "auth/login") {
    const next = request.headers.get("x-knowflow-slot");
    if (Object.keys(data).sort().join(",") !== "email,password" ||
        typeof data.email !== "string" || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(data.email) || data.email.length > 320 ||
        typeof data.password !== "string" || !data.password.length || data.password.length > 128 ||
        !next || !uuid.test(next) || next === current)
      throw new SafeError(422, "请填写有效邮箱和密码，并使用新的登录槽位");
    const response = await issued(await upstream("/auth/login", undefined, data), next);
    // Clear only the previous slot. The public selector is committed by the
    // browser coordinator AFTER the response, never by a delayed Set-Cookie.
    if (current) clearSlot(response, current);
    return response;
  }
  if (Object.keys(data).length) throw new SafeError(422, "此操作不接受表单字段");
  if (!current || request.headers.get("x-knowflow-slot") !== current)
    throw new SafeError(409, "登录身份已变化，请刷新页面");
  if (!refresh || !/^[A-Za-z0-9_-]{43}$/.test(refresh)) throw new SafeError(401, "会话凭证缺失，请重新登录");
  if (path === "auth/logout") {
    try { await upstream("/auth/logout", undefined, { refresh_token: refresh }, true); }
    catch { throw new SafeError(503, "服务端退出尚未确认；请重试退出，或重新登录。当前页面已暂停操作"); }
    return clearSlot(json({ ok: true }), current);
  }
  try { return await issued(await upstream("/auth/refresh", undefined, { refresh_token: refresh }), current); }
  catch (e) {
    // Only this explicit failed rotation may delete THIS slot. No selector write.
    if (e instanceof SafeError && e.status === 401) return clearSlot(error(e), current);
    throw e;
  }
}
