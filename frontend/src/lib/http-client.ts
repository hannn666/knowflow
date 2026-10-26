export class ClientError extends Error {
  constructor(public status: number, message: string, public code?: string) { super(message); }
}
export async function send<T>(path: string, data?: unknown, options?: {
  file?: File; noBody?: boolean; timeoutMs?: number; headers?: Record<string, string>;
}): Promise<T> {
  const form = options?.file ? new FormData() : undefined;
  if (form) form.append("file", options!.file!);
  const writing = data !== undefined || !!form || !!options?.noBody;
  let response: Response;
  try {
    response = await fetch(`/api/${path}`, {
      method: writing ? "POST" : "GET", cache: "no-store", credentials: "same-origin",
      headers: { ...(data === undefined ? {} : { "Content-Type": "application/json" }), ...options?.headers },
      body: form ?? (data === undefined ? undefined : JSON.stringify(data)),
      signal: AbortSignal.timeout(options?.timeoutMs ?? 15000),
    });
  } catch (e) {
    if (writing) throw new ClientError(504, "请求未获确认，结果尚不确定；请刷新状态，不要立即重复操作");
    throw new ClientError(503, e instanceof Error && e.name === "TimeoutError" ? "请求超时，请重试" : "网络连接失败，请稍后重试");
  }
  let payload;
  try { payload = await response.json(); }
  catch { throw new ClientError(503, "服务返回异常，请稍后重试"); }
  if (!response.ok) throw new ClientError(response.status,
    typeof payload?.message === "string" ? payload.message : "请求失败，请重试");
  return payload as T;
}
