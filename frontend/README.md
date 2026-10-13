# KnowFlow Web · M5-1

Next.js App Router + TypeScript + Tailwind CSS。当前只提供登录、个人知识库列表/创建/信息查看及退出，不含注册、文档操作或问答界面。

## 本地运行

Node 24 已验证。依赖/锁文件保存在本目录；npm缓存仅通过命令指定E盘，不改变全局配置。

```powershell
npm ci --cache E:/Dev/Caches/npm
Copy-Item .env.example .env.local
npm run dev
```

在浏览器打开 http://localhost:3000 。FastAPI由用户单独启动在8000，已有账号用于登录。本前端不注册用户、不执行数据库迁移。

`.env.local`中的`KNOWFLOW_API_URL`仅服务端可读、固定可信FastAPI地址；`KNOWFLOW_WEB_ORIGIN`是浏览器地址的精确Origin。主机名/端口要一致，localhost与127.0.0.1不可混用。生产运行必须配置HTTPS Origin，Cookie自动Secure；不信任浏览器Host或Forwarded头确定CSRF边界。反向代理需保留Origin。没有refresh token，15分钟后重新登录。

浏览器只调用同源/api；JWT仅HttpOnly/SameSite=Strict/host-only/path=/ Cookie，未存localStorage或JavaScript。固定BFF allowlist只代理login/me/list/create/UUID详情，所有写请求验证Origin及JSON类型，实际计数表单4096字节限额。所有私有响应/SSR与fetch禁用缓存；401清除Cookie、跳转登录。退出清除浏览器Cookie，不能撤销已被窃取JWT。生产部署的TLS、入口限流和全局XSS/CSRF审计不在本片范围。

知识库每页50项，支持前后分页；名称按文字呈现，不执行用户HTML。短会话及身份刷新避免跨用户复用页面；登录/退出使用整页导航清除路由内存状态。

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

M5-2需要解决的现有接口缺口：文档只有上传和指定版本查询，缺少知识库文档列表/版本列表接口。现有parse和index触发均同步；不要把pending解释为已可问答，不要为页面显示伪造ready状态。

生产Cookie测试使用实际生产Next进程与模拟上游，仅检验Secure/HttpOnly/Strict及固定HTTPS Origin配置；通过本地HTTP发送请求检查响应头，不代表完成真实HTTPS部署验收。next-env.d.ts由Next生成并忽略，typecheck先执行next typegen以支持干净检出。
