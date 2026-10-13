// Explicit HTTP mock for controlled failures; not a real FastAPI/SQL result.
import http from "node:http";
import { randomUUID } from "node:crypto";
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
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, "http://127.0.0.1:8107");
  const send = (status, data) => {
    res.writeHead(status, { "content-type": "application/json" });
    res.end(JSON.stringify(data));
  };
  if (url.pathname === "/health") return send(200, { status: "ok" });
  let data = {};
  try {
    let raw = "";
    for await (const c of req) raw += c;
    data = raw ? JSON.parse(raw) : {};
  } catch {
    return send(422, {});
  }
  if (url.pathname === "/auth/login") {
    const user = users[data.email];
    if (!user || data.password !== "Fixture-only-password-123")
      return send(401, { detail: "private-password-sentinel" });
    const exp =
      Math.floor(Date.now() / 1000) +
      (data.email.startsWith("short") ? 4 : 900);
    return send(200, {
      access_token:
        "fixture." +
        Buffer.from(JSON.stringify({ sub: user.id, exp })).toString(
          "base64url",
        ) +
        ".signature",
      token_type: "bearer",
      expires_in: 900,
    });
  }
  let user;
  try {
    const claims = JSON.parse(
      Buffer.from(
        req.headers.authorization?.split(" ")[1].split(".")[1] ?? "",
        "base64url",
      ).toString(),
    );
    if (claims.exp > Date.now() / 1000)
      user = Object.values(users).find((u) => u.id === claims.sub);
  } catch {}
  if (!user) return send(401, { detail: "private-token-sentinel" });
  if (url.pathname === "/auth/me")
    return send(200, {
      ...user,
      access_token: "private-token-sentinel",
      password_hash: "private-password-sentinel",
    });
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
  const item = spaces.find(
    (x) =>
      url.pathname === "/knowledge-bases/" + x.id && x.owner_id === user.id,
  );
  return item
    ? send(200, item)
    : send(404, { detail: "private-resource-sentinel" });
});
server.listen(8107, "127.0.0.1");
