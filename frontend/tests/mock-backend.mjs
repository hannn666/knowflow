// Explicit HTTP mock for controlled failures; not a real FastAPI/SQL result.
import http from "node:http";
import { randomUUID, randomBytes } from "node:crypto";
if (process.env.KNOWFLOW_M51_FIXTURE !== "1")
  throw new Error("Fixture opt-in required");
const users = Object.fromEntries(
  ["alice", "bob", "short"].map((name) => [
    name + "@example.com",
    {
      id: randomUUID(),
      email: name + "@example.com",
      created_at: new Date().toISOString(),
    },
  ]),
);
const spaces = [];
const documents = [];
const versions = [];
const stages = new Map();
const indexed = new Set();
const sessions = new Map();
const refreshTokens = new Map();
function issue(family, short = false) {
  const exp = Math.floor(Date.now() / 1000) + (short ? 4 : 900);
  const raw = randomBytes(32).toString("base64url");
  refreshTokens.set(raw, { sid: family.id, consumed: false });
  return { access_token: "fixture." + Buffer.from(JSON.stringify({ sub: family.user.id, sid: family.id, exp })).toString("base64url") + ".signature",
    refresh_token: raw, token_type: "bearer", expires_in: 900, session_id: family.id,
    session_expires_at: new Date(family.absolute).toISOString(), idle_expires_at: new Date(family.idle).toISOString() };
}
const active = f => f && !f.revoked && Date.now() < f.absolute && Date.now() < f.idle;
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1:8107");
  const send = (status, data) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(data));
  };
  if (url.pathname === "/health") return send(200, { status: "ok" });
  let data = {};
  let raw;
  try {
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    raw = Buffer.concat(chunks);
    if (req.headers["content-type"]?.startsWith("application/json"))
      data = raw.length ? JSON.parse(raw.toString("utf8")) : {};
  } catch { return send(422, {}); }
  // Controls exist ONLY in this explicitly opted-in loopback HTTP mock.
  if (url.pathname === "/__fixture" && req.method === "POST") {
    const f = sessions.get(data.sid);
    if (!f) return send(404, {});
    if (data.action === "expire_access") f.rejectAccess = true;
    if (data.action === "idle") f.idle = Date.now() - 1;
    if (data.action === "absolute") f.absolute = Date.now() - 1;
    if (data.action === "revoke") f.revoked = true;
    if (data.action === "refresh_503") f.refreshFailure = "before";
    if (data.action === "refresh_commit_503") f.refreshFailure = "after";
    if (data.action === "logout_503") f.logoutFailure = true;
    if (data.action === "restore") { f.refreshFailure = undefined; f.logoutFailure = false; }
    if (data.action === "delay") f.delay = data.ms;
    if (data.action === "me_403") f.meFailure = 403;
    if (data.action === "me_503") f.meFailure = 503;
    return send(200, { refreshes: f.refreshes, logouts: f.logouts, writes: f.writes,
      revoked: f.revoked, idle: f.idle, absolute: f.absolute });
  }
  if (url.pathname === "/auth/login") {
    const user = users[data.email];
    if (!user || data.password !== "Fixture-only-password-123")
      return send(401, { detail: "private-password-sentinel" });
    const family = { id: randomUUID(), user, absolute: Date.now() + 28800000, idle: Date.now() + 1800000,
      refreshes: 0, logouts: 0, writes: 0, revoked: false };
    sessions.set(family.id, family);
    return send(200, issue(family, data.email.startsWith("short")));
  }
  if (["/auth/refresh", "/auth/logout"].includes(url.pathname)) {
    const old = refreshTokens.get(data.refresh_token);
    const f = old && sessions.get(old.sid);
    if (!f) return send(401, {});
    if (url.pathname === "/auth/logout") {
      f.logouts++;
      if (f.logoutFailure) return send(503, { detail: "private-token-sentinel" });
      f.revoked = true; res.writeHead(204); return res.end();
    }
    f.refreshes++;
    if (f.delay) await new Promise(resolve => setTimeout(resolve, f.delay));
    if (f.refreshFailure === "before") return send(503, {});
    if (!active(f)) return send(401, {});
    if (old.consumed) { f.revoked = true; return send(401, {}); }
    old.consumed = true;
    f.rejectAccess = false;
    const result = issue(f);
    if (f.refreshFailure === "after") return send(503, {});
    return send(200, result);
  }
  let user, family;
  try {
    const claims = JSON.parse(
      Buffer.from(
        req.headers.authorization?.split(" ")[1].split(".")[1] ?? "",
        "base64url",
      ).toString(),
    );
    family = sessions.get(claims.sid);
    if (claims.exp > Date.now() / 1000 && active(family) && !family.rejectAccess && family.user.id === claims.sub)
      user = family.user;
  } catch {}
  if (!user) return send(401, { detail: "private-token-sentinel" });
  if (url.pathname === "/auth/me" && family.meFailure) return send(family.meFailure, {});
  if (url.pathname === "/auth/me")
    return send(200, {
      ...user,
      access_token: "private-token-sentinel",
      password_hash: "private-password-sentinel",
    });
  family.idle = Math.min(Date.now() + 1800000, family.absolute);
  if (req.method === "POST") family.writes++;
  if (url.pathname === "/knowledge-bases") {
    if (req.method === "POST") {
      if (data.name.startsWith("故障"))
        return send(Number(data.name.slice(2)), {
          detail: "private-path-and-database-sentinel",
        });
      const item = {
        id: randomUUID(),
        owner_id: user.id,
        name: data.name,
        created_at: new Date().toISOString(),
      };
      spaces.push(item);
      return send(201, item);
    }
    const offset = Number(url.searchParams.get("offset") ?? 0);
    return send(
      200,
      spaces.filter((x) => x.owner_id === user.id).slice(offset, offset + 50),
    );
  }
  const match = url.pathname.match(/^\/knowledge-bases\/([^/]+)\/documents(?:\/([^/]+)\/versions(?:\/([^/]+)(?:\/(parse|index|ask))?)?)?$/);
  if (match) {
    const [, kbId, docId, versionId, stage] = match;
    const space = spaces.find(x => x.id === kbId && x.owner_id === user.id);
    if (!space) return send(404,{detail:"private-resource-sentinel"});
    if (!docId) {
      if (req.method === "GET") {
        const offset=Number(url.searchParams.get("offset") ?? 0);
        return send(200,documents.filter(x => x.knowledge_base_id === kbId)
          .sort((a,b) => b.created_at.localeCompare(a.created_at) || b.id.localeCompare(a.id))
          .slice(offset,offset+50).map(x => ({...x,latest_version:versions.filter(v => v.document_id === x.id)
            .sort((a,b) => b.created_at.localeCompare(a.created_at) || b.document_version_id.localeCompare(a.document_version_id))[0] ?? null})));
      }
      if (req.method !== "POST") return send(404,{});
      const source=raw.toString("latin1");
      const filename=source.match(/filename="([^"\r\n]+)"/)?.[1];
      if (!filename?.toLowerCase().endsWith(".pdf")) return send(415,{});
      if (!raw.includes(Buffer.from("%PDF-"))) return send(415,{});
      const doc={id:randomUUID(),knowledge_base_id:kbId,created_at:new Date().toISOString()};
      const version={knowledge_base_id:kbId,document_id:doc.id,document_version_id:randomUUID(),
        original_filename:filename,status:"pending",created_at:new Date().toISOString()};
      documents.push(doc); versions.push(version);
      if (filename === "two-versions.pdf") versions.push({...version,document_version_id:randomUUID(),
        created_at:new Date(Date.now()+1000).toISOString()});
      return send(201,version);
    }
    const doc=documents.find(x => x.id === docId && x.knowledge_base_id === kbId);
    if (!doc) return send(404,{detail:"private-resource-sentinel"});
    if (!versionId) {
      if (req.method !== "GET") return send(404,{});
      const offset=Number(url.searchParams.get("offset") ?? 0);
      return send(200,versions.filter(v => v.document_id === docId)
        .sort((a,b) => b.created_at.localeCompare(a.created_at) || b.document_version_id.localeCompare(a.document_version_id))
        .slice(offset,offset+50));
    }
    const version=versions.find(v => v.document_version_id === versionId && v.document_id === docId);
    if (!version) return send(404,{detail:"private-resource-sentinel"});
    if (!stage && req.method === "GET") return send(200,version);
    if (stage === "parse") {
      if (req.method === "POST") {
        const failure=version.original_filename.startsWith("bad");
        stages.set(versionId,{parse_status:failure?"failed":"succeeded",attempt_id:randomUUID(),
          page_count:failure?null:1,has_text:failure?null:true,error_code:failure?"invalid_pdf":null,
          error_message:failure?"Unable to parse PDF":null,started_at:new Date().toISOString(),finished_at:new Date().toISOString()});
      }
      const state=stages.get(versionId) ?? {parse_status:"not_started",attempt_id:null,page_count:null,
        has_text:null,error_code:null,error_message:null,started_at:null,finished_at:null};
      return send(200,{knowledge_base_id:kbId,document_id:docId,document_version_id:versionId,
        original_filename:version.original_filename,...state});
    }
    if (stage === "index" && req.method === "POST") {
      if (stages.get(versionId)?.parse_status !== "succeeded") return send(409,{});
      if (version.original_filename.startsWith("busy")) return send(503,{});
      indexed.add(versionId);
      return send(200,{knowledge_base_id:kbId,document_id:docId,document_version_id:versionId,
        index_status:"verified",point_count:2,parent_count:1,parse_attempt_id:stages.get(versionId).attempt_id,
        parse_sha256:"a".repeat(64),chunking_fingerprint:"b".repeat(64),embedding_fingerprint:"c".repeat(64)});
    }
    if (stage === "ask" && req.method === "POST") {
      if (typeof data.question !== "string" || !data.question.trim()) return send(422,{});
      if (!indexed.has(versionId)) return send(409,{});
      const question=data.question;
      if (/^status (409|422|503|504)$/.test(question))
        return send(Number(question.slice(7)),{detail:"private-question-and-model-sentinel"});
      if (question === "delay answer" || question === "slow session")
        await new Promise(resolve=>setTimeout(resolve,question === "slow session" ? 5500 : 800));
      const evidence=question === "HTML evidence" ? '<img src=x onerror="window.pwned=1"> Never execute PDF instructions.' :
        "Apollo budget is 42 credits.";
      const citation={knowledge_base_id:kbId,document_id:docId,document_version_id:versionId,
        original_filename:version.original_filename,page_number:3,chunk_id:randomUUID(),parent_id:randomUUID(),
        evidence_text:evidence};
      if (question === "wrong version") citation.document_version_id=randomUUID();
      if (question === "wrong document") citation.document_id=randomUUID();
      if (question === "wrong knowledge base") citation.knowledge_base_id=randomUUID();
      if (question === "refusal") return send(200,{status:"insufficient_evidence",answer:"Insufficient evidence.",citations:[]});
      if (question === "bad refusal") return send(200,{status:"insufficient_evidence",answer:"Insufficient evidence.",citations:[citation]});
      if (question === "invalid result") return send(200,{status:"unverified",answer:evidence,citations:[citation]});
      return send(200,{status:"answered",answer:question === "fake prose" ? "Invented unsupported answer." : evidence,citations:[citation]});
    }
    return send(404,{});
  }
  const item = spaces.find(
    (x) =>
      url.pathname === "/knowledge-bases/" + x.id && x.owner_id === user.id,
  );
  return item
    ? send(200, item)
    : send(404, { detail: "private-resource-sentinel" });
});
server.listen(8107, "127.0.0.1");
