import "server-only";
import { uuid } from "./contracts";
import { backendBase, checkOrigin, fail, json, me, SafeError, token } from "./backend";
import { isDocument, isParse, isReceipt, isVersion, type DocumentItem, type DocumentVersion, type ParseStage, type IndexReceipt } from "./document-contracts";

const MAX_UPLOAD_REQUEST = 11 * 1024 * 1024;
const PAGE_SIZE = 50;
const unavailable = () => new SafeError(503, "文档服务返回无效结果");

async function bounded(request: Request, max: number): Promise<Uint8Array> {
  const length = request.headers.get("content-length");
  if (length && (!/^\d{1,9}$/.test(length) || Number(length) > max))
    throw new SafeError(413, "上传内容超过大小限制");
  const reader = request.body?.getReader();
  if (!reader) throw new SafeError(422, "请选择 PDF 文件");
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.length;
      if (size > max) {
        await reader.cancel();
        throw new SafeError(413, "上传内容超过大小限制");
      }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  if (!size) throw new SafeError(422, "请选择 PDF 文件");
  const output = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { output.set(chunk, offset); offset += chunk.length; }
  return output;
}
async function empty(request: Request) {
  if (request.headers.get("content-type"))
    throw new SafeError(422, "此操作不接受请求正文");
  const length = request.headers.get("content-length");
  if (length !== null && length !== "0")
    throw new SafeError(422, "此操作不接受请求正文");
  if (length === "0" || !request.body) return;
  const reader = request.body.getReader();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const result = await Promise.race([
      reader.read(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new SafeError(422, "此操作不接受请求正文")), 3000);
      }),
    ]);
    if (!result.done || result.value?.length)
      throw new SafeError(422, "此操作不接受请求正文");
  } finally {
    if (timer) clearTimeout(timer);
    void reader.cancel().catch(() => {});
    try { reader.releaseLock(); } catch { /* A timed-out read may still own the stream. */ }
  }
}
async function backend(path: string, bearer: string, method: "GET" | "POST", timeout: number,
                       body?: Uint8Array, contentType?: string): Promise<unknown> {
  try {
    const response = await fetch(new URL(path, backendBase()), {
      method, cache: "no-store", redirect: "error", signal: AbortSignal.timeout(timeout),
      headers: { Accept: "application/json", Authorization: `Bearer ${bearer}`,
        ...(contentType ? { "Content-Type": contentType } : {}) },
      body: body ? Buffer.from(body) : undefined,
    });
    if (!response.ok) fail(response.status);
    return await response.json() as unknown;
  } catch (e) {
    if (e instanceof SafeError) throw e;
    if (method === "POST") throw new SafeError(504, "请求未获确认，服务端结果尚不确定；请刷新状态，不要立即重复操作");
    throw new SafeError(503, "文档服务暂不可用，请稍后刷新");
  }
}
function page(request: Request) {
  const params = new URL(request.url).searchParams;
  const offset = params.get("offset") ?? "0";
  if (!/^\d{1,7}$/.test(offset) || [...params.keys()].some(k => k !== "offset"))
    throw new SafeError(422, "分页参数无效");
  return `?limit=${PAGE_SIZE}&offset=${offset}`;
}
async function sameUser(request: Request, bearer: string) {
  const expected = request.headers.get("x-knowflow-user");
  if (expected && (await me(bearer)).id !== expected)
    throw new SafeError(409, "登录身份已变化，请刷新页面");
}
function versions(result: unknown, kb: string, doc: string): DocumentVersion[] {
  if (!Array.isArray(result) || result.length > PAGE_SIZE || !result.every(v => isVersion(v, kb, doc)))
    throw unavailable();
  return result.map(v => ({knowledge_base_id:v.knowledge_base_id, document_id:v.document_id,
    document_version_id:v.document_version_id, original_filename:v.original_filename,
    status:v.status, created_at:v.created_at}));
}
function documents(result: unknown, kb: string): DocumentItem[] {
  if (!Array.isArray(result) || result.length > PAGE_SIZE || !result.every(v => isDocument(v, kb)))
    throw unavailable();
  return result.map(v => ({id:v.id, knowledge_base_id:v.knowledge_base_id, created_at:v.created_at,
    latest_version:v.latest_version && versions([v.latest_version], kb, v.id)[0]}));
}
function parse(result: unknown, kb: string, doc: string, ver: string): ParseStage {
  if (!isParse(result,kb,doc,ver)) throw unavailable();
  return {knowledge_base_id:result.knowledge_base_id, document_id:result.document_id,
    document_version_id:result.document_version_id, original_filename:result.original_filename,
    parse_status:result.parse_status, attempt_id:result.attempt_id, page_count:result.page_count,
    has_text:result.has_text, error_code:result.error_code,
    error_message:result.parse_status === "failed" ? "PDF 解析失败，请检查文件后重试" : null,
    started_at:result.started_at, finished_at:result.finished_at};
}
function receipt(result: unknown, kb: string, doc: string, ver: string): IndexReceipt {
  if (!isReceipt(result,kb,doc,ver)) throw unavailable();
  return {knowledge_base_id:result.knowledge_base_id, document_id:result.document_id,
    document_version_id:result.document_version_id, index_status:"verified", point_count:result.point_count,
    parent_count:result.parent_count, parse_attempt_id:result.parse_attempt_id,
    parse_sha256:result.parse_sha256, chunking_fingerprint:result.chunking_fingerprint,
    embedding_fingerprint:result.embedding_fingerprint};
}
export async function documentRoute(request: Request, parts: string[]) {
  const kb = parts[1];
  if (!uuid.test(kb)) throw new SafeError(404, "接口不存在");
  const doc = parts[3], ver = parts[5];
  if (parts.length >= 4 && (parts[2] !== "documents" || !uuid.test(doc))) throw new SafeError(404, "接口不存在");
  if (parts.length >= 6 && (parts[4] !== "versions" || !uuid.test(ver))) throw new SafeError(404, "接口不存在");
  if (request.method === "POST") checkOrigin(request);
  const bearer = await token();
  await sameUser(request, bearer);
  const base = `/knowledge-bases/${kb}/documents`;
  if (parts.length === 3 && request.method === "GET")
    return json(documents(await backend(base+page(request),bearer,"GET",10000),kb));
  if (parts.length === 3 && request.method === "POST") {
    const contentType = request.headers.get("content-type") ?? "";
    if (contentType.length > 200 || /[\r\n]/.test(contentType) ||
        !/^multipart\/form-data;\s*boundary=.{1,70}$/i.test(contentType))
      throw new SafeError(415, "请选择 PDF 文件上传");
    const bytes = await bounded(request,MAX_UPLOAD_REQUEST);
    const result = await backend(base,bearer,"POST",45000,bytes,contentType);
    if (!isVersion(result,kb,(result as DocumentVersion)?.document_id)) throw unavailable();
    return json(versions([result],kb,(result as DocumentVersion).document_id)[0],201);
  }
  if (parts.length === 5 && parts[4] === "versions" && request.method === "GET")
    return json(versions(await backend(`${base}/${doc}/versions${page(request)}`,bearer,"GET",10000),kb,doc));
  if (parts.length === 6 && request.method === "GET") {
    const result = await backend(`${base}/${doc}/versions/${ver}`,bearer,"GET",10000);
    if (!isVersion(result,kb,doc) || result.document_version_id !== ver) throw unavailable();
    return json(versions([result],kb,doc)[0]);
  }
  if (parts.length === 7 && ["parse","index"].includes(parts[6])) {
    const path = `${base}/${doc}/versions/${ver}/${parts[6]}`;
    if (request.method === "GET" && parts[6] === "parse")
      return json(parse(await backend(path,bearer,"GET",10000),kb,doc,ver));
    if (request.method === "POST") {
      await empty(request);
      if (parts[6] === "parse") return json(parse(await backend(path,bearer,"POST",70000),kb,doc,ver));
      return json(receipt(await backend(path,bearer,"POST",300000),kb,doc,ver));
    }
  }
  throw new SafeError(404, "接口不存在");
}
