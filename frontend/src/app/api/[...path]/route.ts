import { authRoute } from "@/lib/auth-backend";
import { uuid } from "@/lib/contracts";
import { documentRoute } from "@/lib/document-backend";
import { askRoute } from "@/lib/grounded-backend";
import {
  SafeError,
  body,
  checkWrite,
  error,
  json,
  kb,
  token,
  sameIdentity,
} from "@/lib/backend";
export const dynamic = "force-dynamic";
export const runtime = "nodejs";
type Context = { params: Promise<{ path: string[] }> };
async function handle(request: Request, context: Context) {
  try {
    const parts = (await context.params).path;
    const path = parts.join("/");
    if (request.method === "POST" && !(parts.length === 3 && parts[2] === "documents" && uuid.test(parts[1] ?? "")) &&
        !(parts.length === 7 && parts[2] === "documents" && parts[4] === "versions" &&
          ["parse", "index"].includes(parts[6]) && uuid.test(parts[1] ?? "") &&
          uuid.test(parts[3] ?? "") && uuid.test(parts[5] ?? ""))) checkWrite(request);
    if (parts[0] === "auth") return await authRoute(request, path);
    if (path === "knowledge-bases") {
      const value = await token();
      await sameIdentity(request, value);
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
      await sameIdentity(request, value);
      return json(await kb(`/knowledge-bases/${parts[1]}`, value));
    }
    if (parts.length === 7 && parts[6] === "ask" && request.method === "POST")
      return await askRoute(request,parts);
    if (parts[0] === "knowledge-bases" && uuid.test(parts[1] ?? "") && parts[2] === "documents")
      return await documentRoute(request, parts);
    throw new SafeError(404, "接口不存在");
  } catch (e) {
    return error(e);
  }
}
export const GET = handle;
export const POST = handle;
