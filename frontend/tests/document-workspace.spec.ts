import { test, expect, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
const real = process.env.M51_REAL === "1";
const credentials = { email:"alice@example.com", password:"Fixture-only-password-123" };
const origin = "http://127.0.0.1:3107";
const fixture = "tests/fixtures/public-m5.pdf";
async function login(page: Page, email=credentials.email) {
  await page.goto("/login");
  await page.getByLabel("邮箱",{exact:true}).fill(email);
  await page.getByLabel("密码",{exact:true}).fill(credentials.password);
  await page.getByRole("button",{name:"进入工作台"}).click();
  await expect(page).toHaveURL(/knowledge-bases$/);
}
async function space(page: Page) {
  await login(page);
  const name=`文档空间-${Date.now()}`;
  await page.getByLabel("知识库名称").fill(name);
  await page.getByRole("button",{name:"创建知识库"}).click();
  await page.getByRole("button").filter({has:page.getByRole("heading",{name,exact:true})}).click();
  await page.getByRole("link",{name:"进入文档管理"}).click();
  await expect(page).toHaveURL(/knowledge-bases\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading",{name,exact:true,level:1})).toBeVisible();
  return new URL(page.url()).pathname.split("/").at(-1)!;
}
async function upload(page: Page, filename=fixture) {
  await page.getByLabel("选择 PDF").setInputFiles(filename);
  await page.getByRole("button",{name:"上传 PDF"}).click();
  await expect(page.getByRole("button",{name:"正在上传"})).toHaveCount(0,{timeout:30000});
  await expect(page.getByRole("button",{name:/public-m5.pdf/}).first()).toBeVisible();
}
test("private document page, real upload/list/parse, and refresh never restores index receipt", async ({page}, testInfo) => {
  test.setTimeout(120000);
  await page.goto("/knowledge-bases/11111111-1111-4111-8111-111111111111");
  await expect(page).toHaveURL(/login/);
  const kb=await space(page);
  await expect(page.getByText("还没有 PDF 文档")).toBeVisible();
  await upload(page);
  await expect(page.getByText("原文件已保存").first()).toBeVisible();
  await expect(page.getByText("尚未解析")).toBeVisible();
  await expect(page.getByRole("button",{name:"建立索引"})).toBeDisabled();
  await page.getByRole("button",{name:"开始解析"}).click();
  await expect(page.locator(".stage-panel p").filter({hasText:"解析阶段：解析成功"})).toBeVisible({timeout:90000});
  if (!real) {
    await page.getByRole("button",{name:"建立索引"}).click();
    await expect(page.getByText(/本次请求已验证 2 个检索块/)).toBeVisible();
  }
  await page.evaluate(() => window.scrollTo(0,0));
  await page.screenshot({path:`test-results/m52-${testInfo.project.name}-${real?"real":"mock"}-viewport.png`});
  await page.screenshot({path:`test-results/m52-${testInfo.project.name}-${real?"real":"mock"}-full.png`,fullPage:true});
  await page.reload();
  await expect(page.getByRole("heading",{name:/文档空间-/})).toBeVisible();
  await expect(page.getByRole("button",{name:/public-m5.pdf/}).first()).toBeVisible();
  await page.getByRole("button",{name:/public-m5.pdf/}).first().click();
  await expect(page.locator(".stage-panel p").filter({hasText:"解析阶段：解析成功"})).toBeVisible();
  await expect(page.getByText("尚无本页有效核验回执")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  expect(kb).toMatch(/^[0-9a-f-]{36}$/);
});
test("cross-account document listing, upload Origin/body limits, and token secrecy", async ({page,context}) => {
  const kb=await space(page);
  const pdf=readFileSync(fixture);
  const route=`/api/knowledge-bases/${kb}/documents`;
  const badOrigin=await context.request.post(route,{headers:{Origin:"https://attacker.example"},
    multipart:{file:{name:"public-m5.pdf",mimeType:"application/pdf",buffer:pdf}}});
  expect(badOrigin.status()).toBe(403);
  const wrongType=await context.request.post(route,{headers:{Origin:origin,"Content-Type":"application/json"},data:{file:"not-pdf"}});
  expect(wrongType.status()).toBe(415);
  const oversized=await context.request.post(route,{headers:{Origin:origin,"Content-Type":"multipart/form-data; boundary=test"},
    data:Buffer.alloc(11*1024*1024+1,65)});
  expect(oversized.status()).toBe(413);
  await upload(page);
  const listed=await context.request.get(route);
  expect(listed.status()).toBe(200);
  expect(listed.headers()["cache-control"]).toContain("no-store");
  const text=await listed.text();
  expect(text).not.toContain("source.pdf"); expect(text).not.toContain("access_token");
  await page.getByRole("button",{name:"退出登录"}).click();
  await expect(page).toHaveURL(/login/);
  await login(page,"bob@example.com");
  const denied=await context.request.get(route);
  expect(denied.status()).toBe(404);
  expect(await denied.text()).not.toContain("public-m5.pdf");
  const foreignPage=await page.goto(`/knowledge-bases/${kb}`);
  expect(foreignPage?.status()).toBe(404);
  await expect(page.getByText("添加 PDF 文档")).toHaveCount(0);
});
test("client validation, duplicate upload and visible waiting state", async ({page}) => {
  await space(page);
  await page.getByLabel("选择 PDF").setInputFiles({name:"wrong.txt",mimeType:"text/plain",buffer:Buffer.from("bad")});
  await page.getByRole("button",{name:"上传 PDF"}).click();
  await expect(page.locator(".error[role=alert]")).toContainText("仅支持不超过 10 MiB 的 PDF");
  await page.getByLabel("选择 PDF").setInputFiles({name:"oversize.pdf",mimeType:"application/pdf",buffer:Buffer.alloc(10*1024*1024+1)});
  await page.getByRole("button",{name:"上传 PDF"}).click();
  await expect(page.locator(".error[role=alert]")).toContainText("10 MiB");
  await page.getByLabel("选择 PDF").setInputFiles(fixture);
  let writes=0;
  await page.route("**/api/knowledge-bases/*/documents",async route => {
    if (route.request().method() !== "POST") return route.continue();
    writes++;
    await new Promise(resolve=>setTimeout(resolve,400));
    await route.continue();
  });
  await page.getByRole("button",{name:"上传 PDF"}).click();
  await expect(page.getByRole("button",{name:"正在上传"})).toBeDisabled();
  await expect(page.getByRole("button",{name:/public-m5.pdf/}).first()).toBeVisible();
  expect(writes).toBe(1);
  await expect(page.getByLabel("选择 PDF")).toHaveValue("");
  await page.getByLabel("选择 PDF").setInputFiles(fixture);
  await expect(page.getByRole("button",{name:"上传 PDF"})).toBeEnabled();
});
test("mock parse failure, 409/503 index and uncertain timeout never show verified", async ({page,context}) => {
  test.skip(real,"Controlled failure injection is only on explicit HTTP mock");
  const kb=await space(page);
  const pdf=readFileSync(fixture);
  const create=async (name:string) => {
    const r=await context.request.post(`/api/knowledge-bases/${kb}/documents`,
      {headers:{Origin:origin},multipart:{file:{name,mimeType:"application/pdf",buffer:pdf}}});
    expect(r.status()).toBe(201); return await r.json();
  };
  const bad=await create("bad.pdf");
  const path=`/api/knowledge-bases/${kb}/documents/${bad.document_id}/versions/${bad.document_version_id}`;
  const bodyRejected=await context.request.post(path+"/parse",{headers:{Origin:origin},data:{unexpected:true}});
  expect(bodyRejected.status()).toBe(422);
  const before=await context.request.post(path+"/index",{headers:{Origin:origin}});
  expect(before.status()).toBe(409);
  const parsed=await context.request.post(path+"/parse",{headers:{Origin:origin}});
  expect((await parsed.json()).parse_status).toBe("failed");
  const busy=await create("busy.pdf");
  const busyPath=`/api/knowledge-bases/${kb}/documents/${busy.document_id}/versions/${busy.document_version_id}`;
  expect((await context.request.post(busyPath+"/parse",{headers:{Origin:origin}})).status()).toBe(200);
  expect((await context.request.post(busyPath+"/index",{headers:{Origin:origin}})).status()).toBe(503);
  await page.getByRole("button",{name:"刷新列表"}).click();
  await page.getByRole("button",{name:/bad.pdf/}).first().click();
  await expect(page.getByText("解析失败")).toBeVisible();
  await page.getByRole("button",{name:/busy.pdf/}).first().click();
  await expect(page.locator(".stage-panel p").filter({hasText:"解析阶段：解析成功"})).toBeVisible();
  await page.route("**/api/knowledge-bases/*/documents/*/versions/*/index", route =>
    route.fulfill({status:504,contentType:"application/json",body:JSON.stringify({message:"请求未获确认，结果尚不确定"})}));
  await page.getByRole("button",{name:"建立索引"}).click();
  await expect(page.locator(".error[role=alert]")).toContainText("结果尚不确定");
  await expect(page.getByText("尚无本页有效核验回执")).toBeVisible();
});

test("mock parse and index stay single-submit while a synchronous request waits", async ({page}) => {
  test.skip(real,"Controlled delayed responses apply only to explicit HTTP mock");
  await space(page); await upload(page);
  let parses=0, indexes=0;
  await page.route("**/api/knowledge-bases/*/documents/*/versions/*/parse", async route => {
    if (route.request().method() !== "POST") return route.continue();
    parses++;
    await new Promise(resolve=>setTimeout(resolve,400));
    await route.continue();
  });
  await page.getByRole("button",{name:"开始解析"}).click();
  await expect(page.getByRole("button",{name:"正在解析"})).toBeDisabled();
  await expect(page.locator(".stage-panel p").filter({hasText:"解析阶段：解析成功"})).toBeVisible();
  expect(parses).toBe(1);
  await page.route("**/api/knowledge-bases/*/documents/*/versions/*/index", async route => {
    if (route.request().method() !== "POST") return route.continue();
    indexes++;
    await new Promise(resolve=>setTimeout(resolve,400));
    await route.continue();
  });
  await page.getByRole("button",{name:"建立索引"}).click();
  await expect(page.getByRole("button",{name:"正在索引"})).toBeDisabled();
  await expect(page.getByText(/本次请求已验证 2 个检索块/)).toBeVisible();
  expect(indexes).toBe(1);
});
