import { test, expect, type Page } from "@playwright/test";
import { appendFileSync, mkdirSync } from "node:fs";
import path from "node:path";
const run=process.env.KNOWFLOW_M53_RUN_ROOT!;
const origin="http://127.0.0.1:3109";
async function login(page:Page,email="m53-alice@example.com") {
  await page.goto("/login");
  await page.getByLabel("邮箱",{exact:true}).fill(email);
  await page.getByLabel("密码",{exact:true}).fill("Fixture-only-password-123");
  await page.getByRole("button",{name:"进入工作台"}).click();
  await expect(page).toHaveURL(/knowledge-bases$/);
}
async function ask(page:Page,question:string) {
  const returned=page.waitForResponse(r=>r.url().endsWith("/ask") && r.request().method()==="POST",{timeout:330000});
  await page.getByLabel("你的问题").fill(question);
  await page.getByRole("button",{name:"提交问题"}).click();
  const response=await returned;
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toContain("no-store");
  return await response.json();
}
test("real browser PDF to Qwen/Qdrant/Ollama answer, physical pages and refusal",async ({page,context},info)=>{
  await login(page);
  const name=`M5-3 public demonstration ${info.project.name}`;
  await page.getByLabel("知识库名称").fill(name);
  await page.getByRole("button",{name:"创建知识库"}).click();
  await page.locator(".kb-card").filter({has:page.getByRole("heading",{name,exact:true})}).getByRole("link",{name:"管理文档"}).click();
  await expect(page).toHaveURL(/knowledge-bases\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading",{name,exact:true,level:1})).toBeVisible();
  const kb=new URL(page.url()).pathname.split("/").at(-1)!;
  const upload=page.waitForResponse(r=>r.url().endsWith(`/knowledge-bases/${kb}/documents`) && r.request().method()==="POST",{timeout:60000});
  await page.getByLabel("选择 PDF").setInputFiles(path.join(run,"public-demo.pdf"));
  await page.getByRole("button",{name:"上传 PDF"}).click();
  const response=await upload; expect(response.status()).toBe(201);
  const v=await response.json();
  const api=`/api/knowledge-bases/${kb}/documents/${v.document_id}/versions/${v.document_version_id}`;
  await expect(page.getByRole("heading",{name:"向此版本提问"})).toBeVisible();
  await page.getByRole("button",{name:"开始解析"}).click();
  await expect(page.locator(".stage-panel p").filter({hasText:"解析阶段：解析成功"})).toBeVisible({timeout:90000});
  await expect(page.locator(".stage-panel p").filter({hasText:"3 页"})).toBeVisible();
  await page.getByRole("button",{name:"建立索引"}).click();
  await expect(page.getByText(/本次请求已验证 2 个检索块/)).toBeVisible({timeout:330000});
  const repeat=await context.request.post(api+"/index",{headers:{Origin:origin},timeout:330000});
  expect(repeat.status()).toBe(200); expect((await repeat.json()).point_count).toBe(2);
  await page.reload(); await page.locator(".document-row").first().click();
  await expect(page.locator(".stage-panel p").filter({hasText:"解析阶段：解析成功"})).toBeVisible();
  const launch=await ask(page,"When did the Apollo project launch?");
  expect(launch.status).toBe("answered"); expect(launch.answer).toContain("2024-04-15");
  expect(launch.citations[0].page_number).toBe(1);
  const budget=await ask(page,"What is the Apollo budget?");
  expect(budget.status).toBe("answered"); expect(budget.answer).toContain("42 credits");
  expect(budget.citations[0].page_number).toBe(3);
  expect(budget.citations.every((c:{document_version_id:string})=>c.document_version_id===v.document_version_id)).toBe(true);
  await expect(page.getByText("第 3 页")).toBeVisible();
  await page.locator(".citation summary").first().click();
  await expect(page.locator(".citation blockquote").first()).toContainText("42 credits");
  const screenshots=path.join(run,"screenshots"); mkdirSync(screenshots,{recursive:true});
  await page.locator(".qa-panel").scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(screenshots,`m53-${info.project.name}-real-viewport.png`)});
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:path.join(screenshots,`m53-${info.project.name}-real-full.png`),fullPage:true});
  const refused=await ask(page,"What is the Apollo project launch location?");
  expect(refused.status).toBe("insufficient_evidence"); expect(refused.citations).toEqual([]);
  await expect(page.getByRole("heading",{name:"当前版本证据不足"})).toBeVisible();
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:path.join(screenshots,`m53-${info.project.name}-real-refusal-full.png`),fullPage:true});
  const wrong=api.replace(v.document_version_id,"00000000-0000-4000-8000-000000000000");
  expect((await context.request.post(wrong+"/ask",{headers:{Origin:origin},data:{question:"Apollo launch?"}})).status()).toBe(404);
  await page.getByRole("button",{name:"退出登录"}).click(); await expect(page).toHaveURL(/login/);
  await login(page,"m53-bob@example.com");
  const denied=await context.request.post(api+"/ask",{headers:{Origin:origin},data:{question:"Apollo launch?"}});
  expect(denied.status()).toBe(404); expect(await denied.text()).not.toContain("2024-04-15");
  appendFileSync(path.join(run,"browser-summary.jsonl"),JSON.stringify({viewport:info.project.name,kb,
    document_id:v.document_id,version_id:v.document_version_id,launch_page:1,budget_page:3,
    refusal:"insufficient_evidence",cross_user_status:404,wrong_version_status:404,index_points:2})+"\n");
});
