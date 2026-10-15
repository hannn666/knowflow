# KnowFlow Web · M5-1 / M5-2 / M5-3

Next.js App Router + TypeScript + Tailwind CSS。当前提供登录、个人知识库列表/创建/详情、PDF 文档与版本列表、上传、手动解析/索引、指定版本摘录式问答与可展开页码证据和退出。不含注册界面或持久化聊天历史。手动启动与演示见 [Windows 指南](DEMO_WINDOWS.md)。

## 本地运行

Node 24 已验证。依赖/锁文件保存在本目录；npm缓存仅通过命令指定E盘，不改变全局配置。

```powershell
npm ci --cache E:/Dev/Caches/npm
Copy-Item .env.example .env.local
npm run dev
```

在浏览器打开 http://localhost:3000 。FastAPI由用户单独启动在8000，已有账号用于登录。本前端不注册用户、不执行数据库迁移。

`.env.local`中的`KNOWFLOW_API_URL`仅服务端可读、固定可信FastAPI地址；`KNOWFLOW_WEB_ORIGIN`是浏览器地址的精确Origin。主机名/端口要一致，localhost与127.0.0.1不可混用。生产运行必须配置HTTPS Origin，Cookie自动Secure；不信任浏览器Host或Forwarded头确定CSRF边界。反向代理需保留Origin。没有refresh token，15分钟后重新登录。

浏览器只调用同源/api；JWT仅HttpOnly/SameSite=Strict/host-only/path=/ Cookie，未存localStorage或JavaScript。固定BFF allowlist代理登录、知识库、文档列表/版本、上传、解析和索引；所有写请求验证Origin。原有JSON表单继续限4096字节，只有固定上传路由接受multipart，实际接收总量限制11MiB，FastAPI另按块限制PDF文件10MiB。所有私有响应/SSR与fetch禁用缓存；401清除Cookie、跳转登录。退出清除浏览器Cookie，不能撤销已被窃取JWT。生产部署的TLS、入口限流和全局XSS/CSRF审计不在本片范围。

知识库、文档与版本每页50项，支持前后分页；名称和文件名按文字呈现，不执行用户HTML。短会话及身份刷新避免跨用户复用页面；登录/退出使用整页导航清除路由内存状态。文档整体pending仅表示原件已保存；解析阶段另行查询；索引verified只显示本次有响应的校验快照，刷新后不会伪造恢复。解析与索引是同步请求，BFF分别等待70秒/300秒，超时可能意味着结果尚不确定，不会自动重复提交。长期生产部署须确认代理平台允许相应请求时长。

## 检查与测试

```powershell
npm run typecheck
npm run lint
npm run build
npx --no-install playwright test --config playwright.production.config.ts
npm test
$env:M51_REAL='1'
npm test
```

Playwright复用本机Edge，不下载浏览器；可通过`KNOWFLOW_BROWSER`指定已安装Chromium可执行路径。测试独占3107前端/8107后端，端口已占用时停止而非复用手动服务。默认明确使用HTTP mock FastAPI；M51_REAL=1启动真实FastAPI路由、Argon2/JWT与临时SQLite，不连接开发PostgreSQL。测试账户只存在于隔离库，短期JWT fixture用于过期验证；密码仅为公开无效测试常量，不是真实凭据。产物位于忽略的test-results；SQLite位于仓库.t隔离目录。测试服务器只由Playwright管理，不停止8000手动服务。

M5-2新增受保护的 `GET /knowledge-bases/{id}/documents` 和 `GET /knowledge-bases/{id}/documents/{document_id}/versions`，不含存储路径。M5-3固定`/ask`路由严格接收question一个字段，实际JSON体≤16KiB；问题≤1000个Unicode字符、≤2000 UTF-8字节；上游响应≤16KiB，引用最多3条，归属ID和原文摘录与当前契约一致才转发。BFF等待300秒、客户端315秒；返回后再次验证JWT，客户端再确认当前身份，过期或身份变化不显示答案。旧版本迟到响应不能写入新版本页面。

默认HTTP Mock验证可控错误和边界，不是真实模型结果。M51_REAL夹具仍仅使用临时SQLite验证上传/解析，不代表PG/索引/模型全链路；M5-3新增独立的真实验收脚本，只在显式设置KNOWFLOW_M53_LIVE=1后新建随机PG库、临时上传根目录及新知识库UUID集合，调用缓存Qwen和现有Ollama。运行方法及资源清理/截图说明见Windows指南。不要直接运行live配置绕过资源所有权守卫。

生产Cookie测试使用实际生产Next进程与模拟上游，仅检验Secure/HttpOnly/Strict及固定HTTPS Origin配置；通过本地HTTP发送请求检查响应头，不代表完成真实HTTPS部署验收。next-env.d.ts由Next生成并忽略，typecheck先执行next typegen以支持干净检出。
