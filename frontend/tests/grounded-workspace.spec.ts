import { apiLogin } from "./auth-helpers";
import { test, expect, type Page, type BrowserContext } from "@playwright/test";
import { readFileSync } from "node:fs";
const origin="http://127.0.0.1:3107";
const pdf=readFileSync("tests/fixtures/public-m5.pdf");
async function login(page:Page,email="alice@example.com") {
  await page.goto("/login");
  await page.getByLabel("邮箱",{exact:true}).fill(email);
  await page.getByLabel("密码",{exact:true}).fill("Fixture-only-password-123");
  await page.getByRole("button",{name:"进入工作台"}).click();
  await expect(page).toHaveURL(/knowledge-bases$/);
}
async function setup(page:Page,context:BrowserContext,two=false,email="alice@example.com") {
  await login(page,email);
  const created=await context.request.post("/api/knowledge-bases",{headers:{Origin:origin},data:{name:"问答工作台"}});
  const kb=(await created.json()).id;
  const uploaded=await context.request.post(`/api/knowledge-bases/${kb}/documents`,{headers:{Origin:origin},
    multipart:{file:{name:two?"two-versions.pdf":"public-m5.pdf",mimeType:"application/pdf",buffer:pdf}}});
  const v=await uploaded.json();
  const path=`/api/knowledge-bases/${kb}/documents/${v.document_id}/versions/${v.document_version_id}`;
  expect((await context.request.post(path+"/parse",{headers:{Origin:origin}})).status()).toBe(200);
  expect((await context.request.post(path+"/index",{headers:{Origin:origin}})).status()).toBe(200);
  await page.goto(`/knowledge-bases/${kb}`);
  await page.locator(".document-row").first().click();
  // Select the uploaded version explicitly when the fixture contains two versions.
  if (two) await page.locator(".version-row").last().click();
  await expect(page.getByRole("heading",{name:"向此版本提问"})).toBeVisible();
  await expect(page.locator(".stage-panel p").filter({hasText:"解析阶段：解析成功"})).toBeVisible();
  return {path,kb,v};
}
async function ask(page:Page,question:string) {
  await page.getByLabel("你的问题").fill(question);
  await page.getByRole("button",{name:"提交问题"}).click();
}
test.beforeEach(() => test.skip(process.env.M51_REAL === "1","Controlled QA responses are explicitly HTTP Mock only"));
test("answer, trusted page, expandable excerpt, refusal and no restored answer",async ({page,context},info) => {
  await setup(page,context);
  await expect(page.getByText("尚无本页有效核验回执")).toBeVisible();
  await ask(page,"Apollo budget?");
  await expect(page.getByRole("heading",{name:"原文摘录回答"})).toBeVisible();
  await expect(page.getByText("第 3 页")).toBeVisible();
  await page.locator(".citation summary").click();
  await expect(page.locator(".citation blockquote")).toHaveText("Apollo budget is 42 credits.");
  await page.locator(".qa-panel").scrollIntoViewIfNeeded();
  await page.screenshot({path:`test-results/m53-${info.project.name}-mock-viewport.png`});
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:`test-results/m53-${info.project.name}-mock-full.png`,fullPage:true});
  await ask(page,"refusal");
  await expect(page.getByRole("heading",{name:"当前版本证据不足"})).toBeVisible();
  await expect(page.locator(".citation")).toHaveCount(0);
  await page.reload(); await page.locator(".document-row").first().click();
  await expect(page.getByRole("heading",{name:"原文摘录回答"})).toHaveCount(0);
});
test("private BFF origin, field/Unicode budgets and cross-user authorization",async ({page,context}) => {
  const {path,kb}=await setup(page,context);
  await ask(page,"中".repeat(667));
  await expect(page.locator(".qa-panel .error")).toContainText("2000 字节");
  expect((await context.request.post(path+"/ask",{headers:{Origin:"https://attacker.example"},data:{question:"Apollo?"}})).status()).toBe(403);
  for (const data of [{question:" "},{question:"中".repeat(667)},{question:"a".repeat(1001)},
    {question:"valid",url:"http://attacker.example"},{question:"\ud800"}]) {
    const r=await context.request.post(path+"/ask",{headers:{Origin:origin},data});
    expect(r.status()).toBe(422); expect(await r.text()).not.toContain("attacker.example");
  }
  const valid=await context.request.post(path+"/ask",{headers:{Origin:origin},data:{question:"中".repeat(666)}});
  expect(valid.status()).toBe(200); expect(valid.headers()["cache-control"]).toContain("no-store");
  expect(await valid.text()).not.toContain("access_token");
  await page.getByRole("button",{name:"退出登录"}).click(); await expect(page).toHaveURL(/login/);
  expect((await context.request.post(path+"/ask",{headers:{Origin:origin},data:{question:"Apollo?"}})).status()).toBe(401);
  await login(page,"bob@example.com");
  expect((await context.request.post(path+"/ask",{headers:{Origin:origin},data:{question:"Apollo?"}})).status()).toBe(404);
  expect((await context.request.post(path.replace(kb,"00000000-0000-4000-8000-000000000000")+"/ask",{headers:{Origin:origin},data:{question:"Apollo?"}})).status()).toBe(404);
});
test("mismatched citations and invalid model response are never rendered",async ({page,context}) => {
  await setup(page,context);
  for (const question of ["wrong version","wrong document","wrong knowledge base","invalid result","bad refusal","fake prose",
    "missing session binding","wrong user binding","wrong session binding"]) {
    await ask(page,question);
    await expect(page.locator(".qa-panel .error")).toContainText("无效结果");
    await expect(page.locator(".qa-result,.citation")).toHaveCount(0);
  }
  await ask(page,"HTML evidence");
  await expect(page.locator(".answer-text")).toContainText("<img");
  await expect(page.locator(".qa-panel img")).toHaveCount(0);
  expect(await page.evaluate(()=>Object.hasOwn(window,"pwned"))).toBe(false);
});
test("409/422/503/504 and disconnected request do not create an answer",async ({page,context}) => {
  await setup(page,context);
  for (const [status,text] of [[409,"重新建立索引"],[422,"问题无效"],[503,"暂不可用"],[504,"未获确认"]] as const) {
    await ask(page,`status ${status}`);
    await expect(page.locator(".qa-panel .error")).toContainText(text);
    await expect(page.locator(".qa-result")).toHaveCount(0);
  }
  await page.route("**/api/knowledge-bases/*/documents/*/versions/*/ask",r=>r.abort());
  await ask(page,"Apollo budget?");
  await expect(page.locator(".qa-panel .error")).toContainText("未获确认");
});
test("single submission and late answer cannot cross a version selection",async ({page,context}) => {
  const {path}=await setup(page,context,true);
  let writes=0;
  await page.route("**/api/knowledge-bases/*/documents/*/versions/*/ask",async r=>{
    writes++; await new Promise(resolve=>setTimeout(resolve,2000)); await r.continue();
  });
  await ask(page,"delay answer");
  await expect(page.getByRole("button",{name:"正在核验与生成"})).toBeDisabled();
  await context.request.post(path+"/parse",{headers:{Origin:origin}});
  await page.getByRole("button",{name:"刷新解析状态"}).click();
  await expect(page.getByLabel("你的问题")).toHaveValue("");
  await expect(page.getByRole("button",{name:"提交问题"})).toBeDisabled();
  await page.locator(".version-row").first().click();
  await expect(page.getByLabel("你的问题")).toHaveValue("");
  await expect(page.getByRole("button",{name:"提交问题"})).toBeDisabled();
  await expect(page.getByRole("button",{name:"提交问题"})).toHaveText("提交问题");
  await expect(page.getByText("当前还有处理请求在执行")).toHaveCount(0,{timeout:10000});
  await expect(page.locator(".qa-result,.citation")).toHaveCount(0);
  expect(writes).toBe(1);
});
test("an account change during an admitted ask never displays the old account answer",async ({page,context}) => {
  await setup(page,context);
  await page.route("**/api/knowledge-bases/*/documents/*/versions/*/ask",async r=>{
    const response=await r.fetch();
    await apiLogin(context, "bob@example.com");
    await r.fulfill({response});
  });
  await ask(page,"Apollo budget?");
  await expect(page).toHaveURL(/knowledge-bases$/);
  await expect(page.locator(".qa-result")).toHaveCount(0);
});
test("expired admission JWT with a valid session still releases verified evidence",async ({page,context}) => {
  const {path}=await setup(page,context);
  await page.getByRole("button",{name:"退出登录"}).click(); await expect(page).toHaveURL(/login/);
  await apiLogin(context,"short@example.com");
  // This user needs its own source; the mock still enforces ownership.
  const kb=await context.request.post("/api/knowledge-bases",{headers:{Origin:origin},data:{name:"short QA"}});
  const id=(await kb.json()).id;
  const upload=await context.request.post(`/api/knowledge-bases/${id}/documents`,{headers:{Origin:origin},
    multipart:{file:{name:"short.pdf",mimeType:"application/pdf",buffer:pdf}}});
  const v=await upload.json(); const own=`/api/knowledge-bases/${id}/documents/${v.document_id}/versions/${v.document_version_id}`;
  await context.request.post(own+"/parse",{headers:{Origin:origin}});
  await context.request.post(own+"/index",{headers:{Origin:origin}});
  const r=await context.request.post(own+"/ask",{headers:{Origin:origin},data:{question:"slow session"}});
  expect(r.status()).toBe(200); expect(await r.text()).toContain("42 credits");
  expect(r.headers()["set-cookie"]).toBeUndefined();
  expect((await (await context.request.get("/api/auth/me")).json()).state).toBe("refresh_required");
  expect((await context.request.post(path+"/ask",{headers:{Origin:origin},data:{question:"Apollo?"}})).status()).toBe(401);
});

async function qaControl(context: BrowserContext, sid: string, action = "inspect") {
  const response = await context.request.post("http://127.0.0.1:8107/__fixture", {data:{sid,action}});
  expect(response.status()).toBe(200); return response.json();
}

test("an admitted answer survives Access expiry and same-session rotation, with one POST", async ({page,context}) => {
  await setup(page,context,false,"qa@example.com");
  const before = await (await context.request.get("/api/auth/me")).json();
  await qaControl(context,before.sessionId,"hold_ask");
  await ask(page,"Apollo budget?");
  await expect.poll(async () => (await qaControl(context,before.sessionId)).waiting).toBe(true);
  await qaControl(context,before.sessionId,"expire_access");
  // No automatic replay: the display check restores credentials AFTER the
  // original POST completes, then confirms the same sid with a fresh /me.
  await qaControl(context,before.sessionId,"release_ask");
  await expect(page.locator(".answer-text")).toContainText("42 credits");
  const after = await (await context.request.get("/api/auth/me")).json();
  expect(after.sessionId).toBe(before.sessionId); expect(after.slot).toBe(before.slot);
  const counts = await qaControl(context,before.sessionId);
  expect(counts.asks).toBe(1); expect(counts.refreshes).toBe(1);
});

test("an answer arriving during another tab's legal rotation waits for that rotation",async ({page,context}) => {
  await setup(page,context,false,"qa@example.com");
  const current = await (await context.request.get("/api/auth/me")).json();
  const other = await context.newPage(); await other.goto("/knowledge-bases");
  await expect(other.getByRole("button",{name:"刷新列表"})).toBeEnabled();
  await page.bringToFront();
  await qaControl(context,current.sessionId,"hold_ask");
  await ask(page,"Apollo budget?");
  await expect.poll(async () => (await qaControl(context,current.sessionId)).waiting).toBe(true);
  await context.request.post("http://127.0.0.1:8107/__fixture", {data:{sid:current.sessionId,action:"delay",ms:1500}});
  await qaControl(context,current.sessionId,"expire_access");
  // Trigger in the background tab without a browser focus/lifecycle change.
  await other.getByRole("button",{name:"刷新列表"}).evaluate(button => (button as HTMLButtonElement).click());
  await expect.poll(async () => (await qaControl(context,current.sessionId)).refreshes).toBe(1);
  await qaControl(context,current.sessionId,"release_ask");
  await expect(page.locator(".answer-text")).toContainText("42 credits");
  expect((await qaControl(context,current.sessionId)).asks).toBe(1);
  expect((await qaControl(context,current.sessionId)).revoked).toBe(false);
});

for (const action of ["revoke","idle","absolute","logout"] as const) {
  test(`an admitted answer cannot survive ${action}`,async ({page,context}) => {
    const {path} = await setup(page,context,false,"qa@example.com");
    const current = await (await context.request.get("/api/auth/me")).json();
    await qaControl(context,current.sessionId,"hold_ask");
    const responsePromise = action === "logout" ? undefined : page.waitForResponse(r => r.url().endsWith(path+"/ask"));
    await ask(page,"Apollo budget?");
    await expect.poll(async () => (await qaControl(context,current.sessionId)).waiting).toBe(true);
    if (action === "logout") {
      // A separate tab uses the real browser coordinator and backend logout.
      const other = await context.newPage(); await other.goto("/knowledge-bases");
      await other.getByRole("button",{name:"退出登录"}).click();
      await expect(other).toHaveURL(/login/);
    } else await qaControl(context,current.sessionId,action);
    await qaControl(context,current.sessionId,"release_ask");
    if (action !== "logout") {
      const response = await responsePromise!;
      expect(response.status()).toBe(401);
      expect(await response.text()).not.toContain("42 credits");
      expect(response.headers()["set-cookie"]).toBeUndefined();
    }
    await expect(page.locator(".qa-result,.citation")).toHaveCount(0);
    expect((await qaControl(context,current.sessionId)).asks).toBe(1);
  });
}

for (const email of ["qa@example.com","bob@example.com"]) {
  for (const outcome of ["answer","ask_401","ask_503","ask_disconnect"]) {
    test(`late ${outcome} cannot cross a new login for ${email}`,async ({page,context}) => {
      await setup(page,context,false,"qa@example.com");
      const old = await (await context.request.get("/api/auth/me")).json();
      await qaControl(context,old.sessionId,"hold_ask");
      if (outcome !== "answer") await qaControl(context,old.sessionId,outcome);
      await ask(page,"Apollo budget?");
      await expect.poll(async () => (await qaControl(context,old.sessionId)).waiting).toBe(true);
      const fresh = await (await apiLogin(context,email)).json();
      expect(fresh.sessionId).not.toBe(old.sessionId);
      await qaControl(context,old.sessionId,"release_ask");
      await expect(page).toHaveURL(/knowledge-bases$/);
      await expect(page.locator(".qa-result,.citation")).toHaveCount(0);
      const current = await (await context.request.get("/api/auth/me")).json();
      expect(current.slot).toBe(fresh.slot); expect(current.sessionId).toBe(fresh.sessionId);
      expect((await qaControl(context,old.sessionId)).asks).toBe(1);
    });
  }
}

test("a revoked session after upstream release is denied by the fresh browser display gate",async ({page,context}) => {
  await setup(page,context,false,"qa@example.com");
  const current = await (await context.request.get("/api/auth/me")).json();
  await page.route("**/api/knowledge-bases/*/documents/*/versions/*/ask",async route => {
    const response = await route.fetch();
    expect(response.status()).toBe(200);
    await qaControl(context,current.sessionId,"revoke");
    await route.fulfill({response});
  });
  await ask(page,"Apollo budget?");
  await expect(page).toHaveURL(/login/);
  await expect(page.locator(".qa-result,.citation")).toHaveCount(0);
  expect((await qaControl(context,current.sessionId)).asks).toBe(1);
});
