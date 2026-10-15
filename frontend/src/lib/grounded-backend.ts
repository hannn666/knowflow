import "server-only";
import { backendBase, body, checkWrite, json, me, SafeError, token } from "./backend";
import { uuid } from "./contracts";
import { checkedAnswer, validQuestion } from "./grounded-contracts";

const MAX_RESPONSE = 16 * 1024;
const invalid = () => new SafeError(503, "问答服务返回无效结果，请稍后重试");
async function decode(response: Response): Promise<unknown> {
  const reader = response.body?.getReader();
  if (!reader) throw invalid();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const {done,value} = await reader.read();
      if (done) break;
      size += value.length;
      if (size > MAX_RESPONSE) { await reader.cancel(); throw invalid(); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk,offset); offset += chunk.length; }
    return JSON.parse(new TextDecoder("utf-8",{fatal:true}).decode(bytes)) as unknown;
  } catch (e) {
    if (e instanceof SafeError) throw e;
    if (e instanceof Error && ["AbortError","TimeoutError"].includes(e.name))
      throw new SafeError(504,"问答请求未获确认，请稍后手动重试；没有生成可显示的答案");
    throw invalid();
  } finally { reader.releaseLock(); }
}
export async function askRoute(request: Request, parts: string[]) {
  checkWrite(request);
  if (request.method !== "POST" || parts.length !== 7 || parts[0] !== "knowledge-bases" ||
      parts[2] !== "documents" || parts[4] !== "versions" || parts[6] !== "ask" ||
      ![parts[1],parts[3],parts[5]].every(v => uuid.test(v)) || new URL(request.url).search)
    throw new SafeError(404,"文档问答接口不存在");
  const [kb,doc,ver] = [parts[1],parts[3],parts[5]];
  const bearer = await token();
  const user = await me(bearer);
  const expected = request.headers.get("x-knowflow-user");
  if (expected && expected !== user.id) throw new SafeError(409,"登录身份已变化，请刷新页面");
  const data = await body(request,16 * 1024);
  if (Object.keys(data).join() !== "question" || !validQuestion(data.question))
    throw new SafeError(422,"问题不能为空，最多 1000 字符且 UTF-8 不超过 2000 字节");
  const url = new URL(`/knowledge-bases/${kb}/documents/${doc}/versions/${ver}/ask`,backendBase());
  let response: Response;
  try {
    response = await fetch(url,{method:"POST",cache:"no-store",redirect:"error",
      headers:{Accept:"application/json","Content-Type":"application/json",Authorization:`Bearer ${bearer}`},
      body:JSON.stringify({question:data.question}),signal:AbortSignal.timeout(300000)});
  } catch {
    throw new SafeError(504,"问答请求未获确认，请稍后手动重试；没有生成可显示的答案");
  }
  if (!response.ok) {
    const messages: Record<number,string> = {
      401:"登录已过期，请重新登录",404:"文档不存在或无权访问",
      409:"当前版本或索引暂不可用于问答，请刷新解析状态并检查或重新建立索引",
      422:"问题无效，请检查字符数和 UTF-8 字节限制",
      503:"模型、后端或处理资源暂不可用，请稍后重试",
      504:"问答请求未获确认，请稍后手动重试；没有生成可显示的答案",
    };
    await response.body?.cancel();
    throw new SafeError(response.status in messages ? response.status : 503,
      messages[response.status] ?? "问答服务暂不可用，请稍后重试");
  }
  const result = checkedAnswer(await decode(response),kb,doc,ver);
  if (!result) throw invalid();
  // A slow model request can outlive the JWT. Revalidate before releasing evidence.
  if ((await me(bearer)).id !== user.id) throw new SafeError(401,"登录已过期，请重新登录");
  return json(result);
}
