export class ClientError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  data?: unknown,
  expectedUser?: string,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/${path}`, {
      method: data === undefined ? "GET" : "POST",
      cache: "no-store",
      credentials: "same-origin",
      headers: {
        ...(data === undefined ? {} : { "Content-Type": "application/json" }),
        ...(expectedUser ? { "X-KnowFlow-User": expectedUser } : {}),
      },
      body: data === undefined ? undefined : JSON.stringify(data),
      signal: AbortSignal.timeout(15000),
    });
  } catch {
    throw new ClientError(503, "网络连接失败，请稍后重试");
  }
  if (response.status === 401 && path !== "auth/login") {
    window.location.replace("/login?expired=1");
    throw new ClientError(401, "登录已过期");
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw new ClientError(503, "服务返回异常，请稍后重试");
  }
  if (
    response.status === 409 &&
    payload.message === "登录身份已变化，请刷新页面"
  ) {
    window.location.replace("/knowledge-bases");
    throw new ClientError(409, payload.message);
  }
  if (!response.ok)
    throw new ClientError(
      response.status,
      payload.message ?? "请求失败，请重试",
    );
  return payload as T;
}
