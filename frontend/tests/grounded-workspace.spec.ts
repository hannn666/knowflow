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
async function setup(page:Page,context:BrowserContext,two=false) {
  await login(page);
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
  for (const question of ["wrong version","wrong document","wrong knowledge base","invalid result","bad refusal","fake prose"]) {
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
test("expired JWT during a slow response cannot release evidence",async ({page,context}) => {
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
  expect(r.status()).toBe(401); expect(await r.text()).not.toContain("42 credits");
  expect((await (await context.request.get("/api/auth/me")).json()).state).toBe("refresh_required");
  expect((await context.request.post(path+"/ask",{headers:{Origin:origin},data:{question:"Apollo?"}})).status()).toBe(401);
});
