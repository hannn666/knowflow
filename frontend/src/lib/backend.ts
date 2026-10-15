import "server-only";
import { cookies } from "next/headers";
import { NextResponse } from "next/server";
import { isUser, isKnowledgeBase } from "./contracts";

export const COOKIE = "knowflow_session";
const MAX_BODY = 4096;
export class SafeError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export function origin() {
  const raw =
    process.env.KNOWFLOW_WEB_ORIGIN ??
    (process.env.NODE_ENV === "production" ? "" : "http://localhost:3000");
  const url = new URL(raw);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.origin !== raw ||
    (process.env.NODE_ENV === "production" && url.protocol !== "https:")
  )
    throw new SafeError(503, "服务配置暂不可用");
  return url.origin;
}
export function backendBase() {
  const url = new URL(process.env.KNOWFLOW_API_URL ?? "http://127.0.0.1:8000");
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    url.pathname !== "/"
  )
    throw new SafeError(503, "服务配置暂不可用");
  return url;
}
export function checkOrigin(request: Request) {
  const site = request.headers.get("sec-fetch-site");
  if (
    request.headers.get("origin") !== origin() ||
    (site && site !== "same-origin")
  )
    throw new SafeError(403, "请求来源无效，请刷新页面重试");
}
export function checkWrite(request: Request) {
  checkOrigin(request);
  if (
    request.headers.get("content-type")?.split(";")[0].trim() !==
    "application/json"
  )
    throw new SafeError(422, "请提交有效表单");
}
export async function body(request: Request, maximum = MAX_BODY): Promise<Record<string, unknown>> {
  const reader = request.body?.getReader();
  if (!reader) throw new SafeError(422, "请填写表单");
  const chunks: Uint8Array[] = [];
  let count = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      count += value.length;
      if (count > maximum) {
        await reader.cancel();
        throw new SafeError(413, "表单内容过长");
      }
      chunks.push(value);
    }
    const all = new Uint8Array(count);
    let offset = 0;
    for (const c of chunks) {
      all.set(c, offset);
      offset += c.length;
    }
    const value: unknown = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(all),
    );
    if (!value || typeof value !== "object" || Array.isArray(value))
      throw new Error();
    return value as Record<string, unknown>;
  } catch (e) {
    if (e instanceof SafeError) throw e;
    throw new SafeError(422, "请提交有效表单");
  } finally {
    reader.releaseLock();
  }
}
export function fail(status: number): never {
  const messages: Record<number, string> = {
    401: "邮箱或密码错误，或登录已过期，请重新登录",
    403: "没有访问权限",
    404: "知识库不存在或无权访问",
    409: "操作发生冲突，请刷新后重试",
    413: "上传内容超过大小限制",
    415: "仅支持 PDF 文件",
    422: "输入信息不符合要求，请检查表单",
  };
  throw new SafeError(
    status in messages ? status : 503,
    messages[status] ?? "后端暂不可用，请稍后重试",
  );
}
export async function upstream(path: string, token?: string, data?: unknown) {
  // Only callers with fixed routes and validated UUIDs may invoke this helper.
  try {
    const response = await fetch(new URL(path, backendBase()), {
      method: data === undefined ? "GET" : "POST",
      redirect: "error",
      cache: "no-store",
      headers: {
        Accept: "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(data !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) fail(response.status);
    return (await response.json()) as unknown;
  } catch (e) {
    if (e instanceof SafeError) throw e;
    throw new SafeError(503, "后端暂不可用，请稍后重试");
  }
}
export async function token() {
  const value = (await cookies()).get(COOKIE)?.value;
  if (!value || value.length > 2048 || !/^[A-Za-z0-9_.-]+$/.test(value))
    throw new SafeError(401, "登录已过期，请重新登录");
  return value;
}
export function expiry(value: string) {
  try {
    const claims = JSON.parse(
      Buffer.from(value.split(".")[1], "base64url").toString(),
    );
    if (typeof claims.exp !== "number" || !Number.isFinite(claims.exp))
      throw new Error();
    // Scheduling only. FastAPI validates JWT signature and authorization every time.
    return Math.min(claims.exp * 1000, Date.now() + 900000);
  } catch {
    throw new SafeError(503, "登录服务返回无效结果");
  }
}
export async function me(value: string) {
  const user = await upstream("/auth/me", value);
  if (!isUser(user)) fail(503);
  return { id: user.id, email: user.email, created_at: user.created_at };
}
export async function kb(path: string, value: string, data?: unknown) {
  const result = await upstream(path, value, data);
  const project = (item: import("./contracts").KnowledgeBase) => ({
    id: item.id,
    owner_id: item.owner_id,
    name: item.name,
    created_at: item.created_at,
  });
  if (path.startsWith("/knowledge-bases?")) {
    if (
      !Array.isArray(result) ||
      result.length > 50 ||
      !result.every(isKnowledgeBase)
    )
      fail(503);
    return result.map(project);
  }
  if (!isKnowledgeBase(result)) fail(503);
  return project(result);
}
export function json(data: unknown, status = 200) {
  return NextResponse.json(data, {
    status,
    headers: { "Cache-Control": "no-store, max-age=0", Pragma: "no-cache" },
  });
}
export function clear(response: NextResponse) {
  response.cookies.set(COOKIE, "", {
    httpOnly: true,
    sameSite: "strict",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: 0,
  });
  return response;
}
export function error(e: unknown) {
  const safe =
    e instanceof SafeError ? e : new SafeError(503, "服务暂不可用，请稍后重试");
  const response = json({ message: safe.message }, safe.status);
  return safe.status === 401 ? clear(response) : response;
}
