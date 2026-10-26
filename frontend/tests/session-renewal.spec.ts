import { test, expect, type Page, type BrowserContext, type APIRequestContext } from "@playwright/test";
import { readFileSync } from "node:fs";
import { apiLogin, credentials, origin } from "./auth-helpers";

test.skip(process.env.M51_REAL === "1", "Controlled races and faults belong to the HTTP mock; real auth uses separate regression tests");
async function login(page: Page, email = credentials.email) {
  await page.goto("/login");
  await page.getByLabel("邮箱", { exact: true }).fill(email);
  await page.getByLabel("密码", { exact: true }).fill(credentials.password);
  await page.getByRole("button", { name: "进入工作台" }).click();
  await expect(page).toHaveURL(/knowledge-bases$/);
  await expect(page.getByRole("heading", { name: "我的知识库" })).toBeVisible();
  await expect(page.getByRole("button", { name: "刷新列表" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "创建知识库" })).toBeDisabled(); // Empty name.
}
async function session(context: BrowserContext) { return (await context.request.get("/api/auth/me")).json(); }
async function control(request: APIRequestContext, sid: string, action = "inspect", ms?: number) {
  const r = await request.post("http://127.0.0.1:8107/__fixture", { data: { sid, action, ms } });
  expect(r.status()).toBe(200); return r.json();
}
const refreshList = (page: Page) => page.getByRole("button", { name: "刷新列表" }).click();

test("dual credentials are private, host-only and scoped; metadata and storage contain no tokens", async ({page, context}) => {
  await login(page);
  const current = await session(context);
  const cookies = await context.cookies();
  const access = cookies.find(c => c.name === `knowflow_access_${current.slot}`)!;
  const refresh = cookies.find(c => c.name === `knowflow_refresh_${current.slot}`)!;
  for (const c of [access, refresh]) { expect(c.httpOnly).toBe(true); expect(c.sameSite).toBe("Strict"); expect(c.domain).toBe("127.0.0.1"); }
  expect(access.path).toBe("/"); expect(refresh.path).toBe("/api/auth");
  expect(access.expires - Date.now()/1000).toBeLessThanOrEqual(900);
  expect(refresh.expires - Date.now()/1000).toBeLessThanOrEqual(28800);
  const response = await context.request.get("/api/auth/me");
  expect(response.headers()["cache-control"]).toContain("no-store");
  const publicText = JSON.stringify(current) + await page.content() + await page.evaluate(() => JSON.stringify({cookie: document.cookie, storage: {...localStorage}}));
  expect(publicText).not.toContain(access.value); expect(publicText).not.toContain(refresh.value);
  let businessCookie = "";
  await page.route("**/api/knowledge-bases?*", route => { businessCookie = route.request().headers()["cookie"] ?? ""; return route.continue(); });
  await refreshList(page);
  await expect.poll(() => businessCookie).toContain(access.name);
  expect(businessCookie).not.toContain(refresh.name);
});

test("same-page and two-tab expiry recovery rotates exactly once and preserves sid", async ({page, context}) => {
  await login(page); const current = await session(context);
  const other = await context.newPage(); await other.goto("/knowledge-bases");
  await expect(other.getByRole("button", {name:"刷新列表"})).toBeEnabled();
  await control(context.request, current.sessionId, "expire_access");
  await Promise.all([refreshList(page), refreshList(other), page.evaluate(() => window.dispatchEvent(new Event("pageshow")))]);
  await expect.poll(async () => (await control(context.request, current.sessionId)).refreshes).toBe(1);
  const after = await session(context); expect(after.sessionId).toBe(current.sessionId);
  expect(after.slot).toBe(current.slot);
  await expect(page.locator(".error")).toHaveCount(0); await expect(other.locator(".error")).toHaveCount(0);
});

test("hard refresh with missing Access cookie recovers through SSR transition", async ({page, context}) => {
  await login(page); const current = await session(context);
  await context.clearCookies({name: `knowflow_access_${current.slot}`});
  await page.reload();
  await expect(page).toHaveURL(/knowledge-bases$/);
  await expect(page.getByRole("heading", {name:"我的知识库"})).toBeVisible();
  expect((await control(context.request, current.sessionId)).refreshes).toBe(1);
});

test("wake and pageshow re-read cookies and recover a sleeping tab", async ({page, context}) => {
  await login(page); const current = await session(context);
  await context.clearCookies({name: `knowflow_access_${current.slot}`});
  await page.evaluate(() => { window.dispatchEvent(new Event("pageshow")); document.dispatchEvent(new Event("visibilitychange")); });
  await expect.poll(async () => (await control(context.request, current.sessionId)).refreshes).toBe(1);
  await refreshList(page); await expect(page).toHaveURL(/knowledge-bases$/);
});

for (const reason of ["idle", "absolute", "revoke"] as const) {
  test(`server ${reason} deadline cannot be bypassed by refresh`, async ({page, context}) => {
    await login(page); const current = await session(context);
    await control(context.request, current.sessionId, reason);
    await refreshList(page);
    await expect(page).toHaveURL(/login/);
    expect((await control(context.request, current.sessionId)).refreshes).toBe(1);
    expect((await context.cookies()).some(c => c.name === `knowflow_refresh_${current.slot}`)).toBe(false);
  });
}

test("replaying an old refresh secret revokes the family and never leaks credentials", async ({context}) => {
  const first = await apiLogin(context); const current = await first.json();
  const old = (await context.cookies()).find(c => c.name === `knowflow_refresh_${current.slot}`)!;
  const headers = {Origin:origin, "X-KnowFlow-Slot":current.slot};
  expect((await context.request.post("/api/auth/refresh", {headers,data:{}})).status()).toBe(200);
  const replay = await context.request.post("http://127.0.0.1:8107/auth/refresh", {data:{refresh_token:old.value}});
  expect(replay.status()).toBe(401);
  const denied = await context.request.post("/api/auth/refresh", {headers,data:{}});
  expect(denied.status()).toBe(401); expect(await denied.text()).not.toContain(old.value);
  expect((await control(context.request,current.sessionId)).revoked).toBe(true);
});

for (const action of ["refresh_503", "refresh_commit_503"] as const) {
  test(`${action}: indeterminate rotation blocks further refresh across tabs and reload`, async ({page,context}) => {
    await login(page); const current = await session(context);
    await control(context.request,current.sessionId,action);
    await control(context.request,current.sessionId,"expire_access");
    await refreshList(page);
    await expect(page.locator(".error")).toContainText("续期结果尚未确认");
    await refreshList(page);
    const other = await context.newPage(); await other.goto("/knowledge-bases");
    await expect(other.locator(".error")).toContainText("续期结果尚未确认");
    await page.reload(); await expect(page.locator(".error")).toContainText("续期结果尚未确认");
    expect((await control(context.request,current.sessionId)).refreshes).toBe(1);
    expect((await context.cookies()).some(c => c.name === `knowflow_refresh_${current.slot}`)).toBe(true);
  });
}

test("network loss after rotation never retries a consumed secret", async ({page,context}) => {
  await login(page); const current = await session(context);
  let calls = 0;
  await page.route("**/api/auth/refresh", async route => { calls++; await route.fetch(); await route.abort(); });
  await control(context.request,current.sessionId,"expire_access");
  await refreshList(page); await expect(page.locator(".error")).toContainText("续期结果尚未确认");
  await refreshList(page); expect(calls).toBe(1);
});

for (const status of [403,503]) {
  test(`metadata ${status} is distinct from expiry and never triggers refresh or cookie clearing`, async ({page,context}) => {
    await login(page); const current = await session(context);
    await control(context.request,current.sessionId,`me_${status}`);
    await refreshList(page); await expect(page.locator(".error")).toBeVisible();
    expect((await control(context.request,current.sessionId)).refreshes).toBe(0);
    expect((await context.cookies()).some(c => c.name === `knowflow_access_${current.slot}`)).toBe(true);
    await expect(page).toHaveURL(/knowledge-bases$/);
  });
}

test("logout failure retains credentials, pauses operations, then explicit retry revokes and notifies all tabs", async ({page,context}) => {
  await login(page); const current = await session(context);
  const other=await context.newPage(); await other.goto("/knowledge-bases");
  await control(context.request,current.sessionId,"logout_503");
  await page.getByRole("button",{name:"退出登录"}).click();
  await expect(page.locator(".error")).toContainText("服务端退出尚未确认");
  expect((await context.cookies()).some(c=>c.name===`knowflow_refresh_${current.slot}`)).toBe(true);
  await refreshList(page); await expect(page.locator(".error")).toContainText("服务端退出尚未确认");
  await control(context.request,current.sessionId,"restore");
  await page.getByRole("button",{name:"退出登录"}).click();
  await expect(page).toHaveURL(/login/); await expect(other).toHaveURL(/login/);
  expect((await control(context.request,current.sessionId)).revoked).toBe(true);
  expect((await context.cookies()).some(c=>c.name.startsWith("knowflow_access_")||c.name.startsWith("knowflow_refresh_"))).toBe(false);
});

for (const email of ["alice@example.com","bob@example.com"]) {
  test(`delayed refresh cannot overwrite a newer ${email} login`, async ({page,context}) => {
    await login(page); const before=await session(context);
    // Deliberately bypass the browser lock with an API login to force the race.
    let release!: () => void; const gate = new Promise<void>(resolve=>{release=resolve;});
    let arrived!: () => void; const ready = new Promise<void>(resolve=>{arrived=resolve;});
    await page.route("**/api/auth/refresh",async route=>{ const response=await route.fetch(); arrived(); await gate; await route.fulfill({response}); });
    await control(context.request,before.sessionId,"expire_access");
    await refreshList(page); await ready;
    const newer=await (await apiLogin(context,email)).json();
    const navigation = page.waitForEvent("framenavigated", frame => frame === page.mainFrame());
    release(); await navigation;
    await expect(page).toHaveURL(/knowledge-bases$/);
    await expect.poll(async()=> (await session(context)).sessionId).toBe(newer.sessionId);
    expect(newer.sessionId).not.toBe(before.sessionId);
    await expect(page.locator(".account .email")).toHaveText(email);
  });
}

test("a late business 401 has no Set-Cookie and cannot erase a new login", async ({page,context}) => {
  await login(page); const before=await session(context);
  let release!: () => void; const gate=new Promise<void>(r=>{release=r;});
  let arrived!: () => void; const ready=new Promise<void>(r=>{arrived=r;});
  await page.route("**/api/knowledge-bases?*",async route=>{arrived();await gate;await route.fulfill({status:401,json:{message:"expired"}});});
  await refreshList(page); await ready;
  const newer=await (await apiLogin(context)).json(); release();
  await expect.poll(async()=> (await session(context)).sessionId).toBe(newer.sessionId);
  expect(newer.sessionId).not.toBe(before.sessionId);
  await page.unroute("**/api/knowledge-bases?*");
  await page.reload(); await expect(page).toHaveURL(/knowledge-bases$/);
  const stale=await context.request.get("/api/knowledge-bases",{headers:{"X-KnowFlow-Slot":before.slot,"X-KnowFlow-Session":before.sessionId}});
  expect(stale.status()).toBe(409); expect(stale.headers()["set-cookie"]).toBeUndefined();
});

test("login waits for an in-flight rotation under the same Web Lock", async ({page,context})=>{
  await login(page); const current=await session(context);
  await control(context.request,current.sessionId,"delay",800);
  await control(context.request,current.sessionId,"expire_access");
  const other=await context.newPage(); await other.goto("/login");
  await other.getByLabel("邮箱",{exact:true}).fill("bob@example.com");
  await other.getByLabel("密码",{exact:true}).fill(credentials.password);
  let refreshEnd=0,loginStart=0;
  page.on("response",r=>{if(r.url().endsWith("/api/auth/refresh"))refreshEnd=Date.now();});
  other.on("request",r=>{if(r.url().endsWith("/api/auth/login"))loginStart=Date.now();});
  await refreshList(page);
  await other.getByRole("button",{name:"进入工作台"}).click();
  await expect(other).toHaveURL(/knowledge-bases$/);
  expect(refreshEnd).toBeGreaterThan(0); expect(loginStart).toBeGreaterThanOrEqual(refreshEnd);
  expect((await session(context)).user.email).toBe("bob@example.com");
});

test("without Web Locks authentication fails clearly and does not issue credentials",async({page})=>{
  await page.addInitScript(()=>Object.defineProperty(navigator,"locks",{value:undefined}));
  let calls=0; page.on("request",r=>{if(r.url().endsWith("/api/auth/login"))calls++;});
  await page.goto("/login");
  await page.getByLabel("邮箱",{exact:true}).fill(credentials.email);
  await page.getByLabel("密码",{exact:true}).fill(credentials.password);
  await page.getByRole("button",{name:"进入工作台"}).click();
  await expect(page.locator(".error")).toContainText("Web Locks"); expect(calls).toBe(0);
});

test("logout queues behind rotation and revokes the newly rotated family",async({page,context})=>{
  await login(page); const current=await session(context);
  const other=await context.newPage(); await other.goto("/knowledge-bases");
  await expect(other.getByRole("button",{name:"刷新列表"})).toBeEnabled();
  await control(context.request,current.sessionId,"delay",500);
  await control(context.request,current.sessionId,"expire_access");
  await refreshList(page);
  await expect.poll(async()=> (await control(context.request,current.sessionId)).refreshes).toBe(1);
  await other.getByRole("button",{name:"退出登录"}).click();
  await expect(other).toHaveURL(/login/); await expect(page).toHaveURL(/login/);
  const state=await control(context.request,current.sessionId);
  expect(state.refreshes).toBe(1); expect(state.logouts).toBe(1); expect(state.revoked).toBe(true);
});

test("a delayed logout response only clears its old slot after a new login",async({page,context})=>{
  await login(page); const old=await session(context);
  let release!:()=>void; const gate=new Promise<void>(r=>{release=r;});
  let arrived!:()=>void; const ready=new Promise<void>(r=>{arrived=r;});
  await page.route("**/api/auth/logout",async route=>{const response=await route.fetch();arrived();await gate;await route.fulfill({response});});
  await page.getByRole("button",{name:"退出登录"}).click(); await ready;
  const newer=await (await apiLogin(context,"bob@example.com")).json(); release();
  await expect.poll(async()=> (await session(context)).sessionId).toBe(newer.sessionId);
  const proof=await context.newPage(); await proof.goto("/knowledge-bases");
  await expect(proof.locator(".account .email")).toHaveText("bob@example.com");
  expect((await control(context.request,old.sessionId)).revoked).toBe(true);
});

test("a delayed login cannot commit its selector over a newer login",async({page,context})=>{
  await apiLogin(context);
  await page.goto("/login");
  let release!:()=>void; const gate=new Promise<void>(r=>{release=r;});
  let arrived!:()=>void; const ready=new Promise<void>(r=>{arrived=r;});
  await page.route("**/api/auth/login",async route=>{const response=await route.fetch();arrived();await gate;await route.fulfill({response});});
  await page.getByLabel("邮箱",{exact:true}).fill(credentials.email);
  await page.getByLabel("密码",{exact:true}).fill(credentials.password);
  await page.getByRole("button",{name:"进入工作台"}).click(); await ready;
  const newer=await (await apiLogin(context,"bob@example.com")).json(); release();
  await expect(page.locator(".error")).toContainText("登录身份已变化");
  expect((await session(context)).sessionId).toBe(newer.sessionId);
  const proof=await context.newPage(); await proof.goto("/knowledge-bases");
  await expect(proof.locator(".account .email")).toHaveText("bob@example.com");
});

test("auth mutation CSRF, method, payload and stale-slot guards",async({context})=>{
  const current=await (await apiLogin(context)).json();
  for(const path of ["refresh","logout"]) {
    for(const headers of [{Origin:"https://attacker.example"},{Origin:origin,"Sec-Fetch-Site":"cross-site"},{}] as Record<string,string>[]) {
      expect((await context.request.post(`/api/auth/${path}`,{headers,data:{}})).status()).toBe(403);
    }
    expect((await context.request.get(`/api/auth/${path}`)).status()).toBe(404);
    expect((await context.request.post(`/api/auth/${path}`,{headers:{Origin:origin,"X-KnowFlow-Slot":current.slot},data:{refresh_token:"private-sentinel"}})).status()).toBe(422);
    expect((await context.request.post(`/api/auth/${path}`,{headers:{Origin:origin,"X-KnowFlow-Slot":"stale"},data:{}})).status()).toBe(409);
  }
});

for(const returnTo of ["https://attacker.example","//attacker.example","/login","/knowledge-bases/../../login","/%2f%2fattacker.example"]) {
  test(`SSR return path rejects ${returnTo}`,async({page,context})=>{
    await apiLogin(context);
    await page.goto(`/session/renew?returnTo=${encodeURIComponent(returnTo)}`);
    await expect(page).toHaveURL(`${origin}/knowledge-bases`);
  });
}

for(const kind of ["create","upload","parse","index","ask"] as const) {
  test(`${kind} POST is never replayed after 401 or an uncertain response`,async({page,context})=>{
    await login(page);
    let endpoint="/api/knowledge-bases";
    if(kind!=="create") {
      const space=await context.request.post(endpoint,{headers:{Origin:origin},data:{name:`no-replay-${kind}-${Date.now()}`}});
      const kb=(await space.json()).id;
      endpoint=`/api/knowledge-bases/${kb}/documents`;
      if(kind!=="upload") {
        const upload=await context.request.post(endpoint,{headers:{Origin:origin},multipart:{file:{name:"public.pdf",mimeType:"application/pdf",buffer:readFileSync("tests/fixtures/public-m5.pdf")}}});
        const v=await upload.json(); const base=`${endpoint}/${v.document_id}/versions/${v.document_version_id}`;
        await context.request.post(base+"/parse",{headers:{Origin:origin}});
        await context.request.post(base+"/index",{headers:{Origin:origin}});
        endpoint=base+`/${kind}`;
      }
      await page.goto(`/knowledge-bases/${kb}`);
      if(kind!=="upload") {
        await page.getByRole("button",{name:/public.pdf/}).first().click();
        await expect(page.locator(".stage-panel p").filter({hasText:"解析阶段：解析成功"})).toBeVisible();
      }
    }
    let writes=0;
    await page.route(`**${endpoint}`,async route=>{
      if(route.request().method()!=="POST")return route.continue();
      writes++; if(kind==="upload"||kind==="ask")await route.abort();
      else await route.fulfill({status:401,json:{message:"expired"}});
    });
    if(kind==="create") { await page.getByLabel("知识库名称").fill("single attempt");await page.getByRole("button",{name:"创建知识库"}).click(); }
    if(kind==="upload") { await page.getByLabel("选择 PDF").setInputFiles("tests/fixtures/public-m5.pdf");await page.getByRole("button",{name:"上传 PDF"}).click(); }
    if(kind==="parse") await page.getByRole("button",{name:"开始解析"}).click();
    if(kind==="index") await page.getByRole("button",{name:"建立索引"}).click();
    if(kind==="ask") { await page.getByLabel("你的问题").fill("Apollo?");await page.getByRole("button",{name:"提交问题"}).click(); }
    await expect(page.locator(".error").first()).toBeVisible(); expect(writes).toBe(1);
  });
}
