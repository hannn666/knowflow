import { uuid } from "@/lib/contracts";
import {
  COOKIE,
  SafeError,
  body,
  checkWrite,
  clear,
  error,
  expiry,
  json,
  kb,
  me,
  token,
  upstream,
} from "@/lib/backend";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
type Context = { params: Promise<{ path: string[] }> };
async function handle(request: Request, context: Context) {
  try {
    const parts = (await context.params).path;
    const path = parts.join("/");
    if (request.method === "POST") checkWrite(request);
    if (path === "auth/logout" && request.method === "POST")
      return clear(json({ ok: true }));
    if (path === "auth/login" && request.method === "POST") {
      const data = await body(request);
      if (
        Object.keys(data).sort().join(",") !== "email,password" ||
        typeof data.email !== "string" ||
        !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(data.email) ||
        data.email.length > 320 ||
        typeof data.password !== "string" ||
        !data.password.length ||
        data.password.length > 128
      )
        throw new SafeError(422, "请填写有效邮箱和密码");
      const raw = (await upstream("/auth/login", undefined, data)) as {
        access_token?: unknown;
        expires_in?: unknown;
        token_type?: unknown;
      };
      if (
        typeof raw?.access_token !== "string" ||
        raw.access_token.length > 2048 ||
        !/^[A-Za-z0-9_.-]+$/.test(raw.access_token) ||
        raw.token_type !== "bearer" ||
        typeof raw.expires_in !== "number" ||
        raw.expires_in <= 0 ||
        raw.expires_in > 900
      )
        throw new SafeError(503, "登录服务返回无效结果");
      const user = await me(raw.access_token);
      const expiresAt = expiry(raw.access_token);
      const age = Math.floor(
        Math.min(raw.expires_in, (expiresAt - Date.now()) / 1000),
      );
      if (age <= 0) throw new SafeError(401, "登录已过期，请重新登录");
      const response = json({ user, expiresAt });
      response.cookies.set(COOKIE, raw.access_token, {
        httpOnly: true,
        sameSite: "strict",
        secure: process.env.NODE_ENV === "production",
        maxAge: age,
        path: "/",
      });
      return response;
    }
    if (path === "auth/me" && request.method === "GET") {
      const value = await token();
      return json({ user: await me(value), expiresAt: expiry(value) });
    }
    if (path === "knowledge-bases") {
      const value = await token();
      const expected = request.headers.get("x-knowflow-user");
      if (expected && (await me(value)).id !== expected)
        throw new SafeError(409, "登录身份已变化，请刷新页面");
      if (request.method === "GET") {
        const query = new URL(request.url).searchParams;
        const offset = query.get("offset") ?? "0";
        if (
          !/^\d{1,7}$/.test(offset) ||
          [...query.keys()].some((k) => k !== "offset")
        )
          throw new SafeError(422, "分页参数无效");
        return json(
          await kb(`/knowledge-bases?limit=50&offset=${offset}`, value),
        );
      }
      const data = await body(request);
      if (
        Object.keys(data).join() !== "name" ||
        typeof data.name !== "string" ||
        !data.name.trim() ||
        [...data.name.trim()].length > 200
      )
        throw new SafeError(422, "名称需为1至200个字符");
      return json(
        await kb("/knowledge-bases", value, { name: data.name.trim() }),
        201,
      );
    }
    if (
      parts.length === 2 &&
      parts[0] === "knowledge-bases" &&
      uuid.test(parts[1]) &&
      request.method === "GET"
    ) {
      const value = await token();
      const expected = request.headers.get("x-knowflow-user");
      if (expected && (await me(value)).id !== expected)
        throw new SafeError(409, "登录身份已变化，请刷新页面");
      return json(await kb(`/knowledge-bases/${parts[1]}`, value));
    }
    throw new SafeError(404, "接口不存在");
  } catch (e) {
    return error(e);
  }
}
export const GET = handle;
export const POST = handle;
