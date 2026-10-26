# KnowFlow Windows 本地启动与演示

当前界面支持登录 → 知识库 → PDF/版本 → 手动解析/索引 → 指定版本摘录式问答与页码引用。问答不是多轮聊天；刷新会清除未持久化的答案和索引回执。

## 1. 检查现有服务（PowerShell）

```powershell
Set-Location E:\Dev\knowflow
$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
$dockerPath = if ($dockerCommand) { $dockerCommand.Source } else {
  Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
}
if (-not (Test-Path -LiteralPath $dockerPath)) { throw '未找到现有 Docker CLI，请核对 Docker Desktop 安装位置' }
& $dockerPath info
& $dockerPath ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'
& $dockerPath volume ls
```

Engine 未连接时先打开已安装的 Docker Desktop，等待引擎就绪。核对容器仍是 `knowflow-postgres-1`（postgres:18.6）和 `knowflow-qdrant-1`（qdrant 1.19.2），卷仍是 `knowflow_postgres_data`、`knowflow_qdrant_data`。仅在原容器停止时启动它们：

```powershell
& $dockerPath start knowflow-postgres-1 knowflow-qdrant-1
& $dockerPath exec knowflow-postgres-1 pg_isready -U knowflow -d knowflow
(Invoke-WebRequest http://127.0.0.1:6333 -UseBasicParsing).StatusCode
ollama list
(Invoke-WebRequest http://127.0.0.1:11434/api/tags -UseBasicParsing).StatusCode
```

确认 `granite4.1:8b` 已存在，不执行 pull。Qwen Embedding 使用现有本机缓存，生产实现只允许离线加载；缺缓存会安全失败，不应为了演示自动重复下载。不要使用 down -v、prune、清库或移动数据卷。

## 2. FastAPI（VS Code 终端一）

已有 `.env` 必须配置 PostgreSQL 密码及 64 个随机十六进制字符的 `KNOWFLOW_JWT_SECRET`。不在终端打印它们，不覆盖已有文件。只读核对迁移版本。本认证分支代码head为`e5d6a7b8c901`，开发库仍为`c842a9f713e0`，当前必须停止本分支应用启动；等待独立授权升级，不运行下面的业务演示。此流程绝不自动升级或回退开发库：

```powershell
Set-Location E:\Dev\knowflow
.venv\Scripts\python.exe -m alembic current
Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
.venv\Scripts\python.exe -m uvicorn project.api.app:create_app --factory --host 127.0.0.1 --port 8000 --workers 1
```

8000 若已有你的服务，不重复启动或关闭它；可以使用现有服务，或改用空闲端口并同步前端服务端地址。保持 Uvicorn 终端可见。另一个终端可检查 `http://127.0.0.1:8000/health`，Swagger 为 `/docs`。推荐单 worker；索引与问答的处理容量不是分布式队列。

## 3. Next.js（终端二）

```powershell
Set-Location E:\Dev\knowflow\frontend
if (-not (Test-Path .env.local)) { Copy-Item .env.example .env.local }
npm run dev
```

仅在首次检出缺依赖时运行 `npm ci --cache E:/Dev/Caches/npm`。确认 `.env.local` 中两个非敏感地址为：

```dotenv
KNOWFLOW_API_URL=http://127.0.0.1:8000
KNOWFLOW_WEB_ORIGIN=http://localhost:3000
```

在浏览器打开 **http://localhost:3000**，不要混用 127.0.0.1 与 localhost。地址配置只有服务端读取，浏览器不持有可读取的 JWT；开发 HTTP Cookie 不带 Secure，生产模式始终带 Secure，必须使用 HTTPS Origin。以上是本地开发方式，不代表已经验收云部署/TLS或300秒代理超时。

## 4. 人工操作顺序

使用专用演示账号登录。网页没有注册入口；账号通过已有 Swagger 注册接口准备。人工新增账号/知识库/上传会写入当前连接数据库，操作前确认目标；自动验收脚本使用独立库，完全不写开发业务数据。不要删除或修改原有账号、知识库及 PDF。

1. 创建一个明确标为演示用途的知识库，点击“管理文档”。
2. 上传不超过10 MiB的公开/自建PDF。上传后整体pending只表示原件已保存。
3. 选择文档和版本，点击“开始解析”，等待解析阶段显示成功及实际页数。
4. 点击“建立索引”，等待本次verified回执；失败/超时不能视作完成，不自动重复提交。
5. 在“向此版本提问”输入问题。问题不能全空白，最多1000字符、UTF-8最多2000字节；中文往往先达到字节限制。
6. 等待模型返回。查看原文摘录回答，展开引用，核对文件名、PDF物理页码和证据原文。
7. 提问文档不支持的问题，预期“当前版本证据不足”，没有假引用。
8. 切换版本会清除问题和答案；刷新后没有持久化历史。即使本地索引回执消失也可尝试问答，后端会重新验证；409时检查解析和重新索引。

用于自动验收的公开三页PDF：第1页 `Apollo project launched on 2024-04-15.`，第2页空白，第3页 `Apollo budget is 42 credits.`。可演示：

- `When did the Apollo project launch?`：预期第1页日期摘录。
- `What is the Apollo budget?`：预期第3页预算摘录；空白页不重编号。
- `What is the Apollo project launch location?`：未给地点，预期证据不足。

摘录有出处不代表模型能保证任意问题都正确；这些固定问题属于本次演示验证，后续评测仍有必要。第二阶段已接入15分钟Access的自动续期，服务端仍执行30分钟空闲及8小时绝对期限；需要支持Web Locks并允许站点存储的浏览器。续期结果不确定时暂停操作，必须退出重试或重登。当前仅隔离环境验证，开发库未迁移，尚未完成真实业务浏览器验收。长问答返回后BFF仍验证原JWT，过期时不会展示证据；第三阶段再处理长请求复验，不能将本轮描述为已解决所有长问答到期问题。

## 5. 安全复现真实自动验收

先确保现有Docker服务、Ollama模型与Qwen缓存可用，8109和3109空闲。脚本只新建独立随机PG数据库，在其中升级schema并创建专用公开fixture账号；使用独立上传根目录和新知识库UUID集合。不会在开发库创建记录或迁移，不下载模型。

```powershell
Set-Location E:\Dev\knowflow
$env:KNOWFLOW_M53_LIVE='1'
.venv\Scripts\python.exe frontend/tests/run_live_browser.py
```

浏览器测试复用本机Edge，不下载浏览器。脚本打印 `.t/m53/<run-id>/summary.json` 的位置，其中保留服务端真实模型调用摘要、原业务数据前后摘要、浏览器验收与清理结果；截图和公开PDF也保留在该运行目录。正常结束会停止仅本脚本的8109服务、删除本轮随机库/所属UUID集合和上传产物；原容器继续运行。失败时查看实际清理结果，不自行清理旧库/共享卷。

## 6. 最短排查

| 表现 | 检查与下一步 |
| --- | --- |
| 登录401 | 核对账号、密码及JWT到期；重新登录，不从浏览器读取或打印Cookie |
| 写请求403 | 浏览器Origin与KNOWFLOW_WEB_ORIGIN必须完全相同；修改配置后重启你自己的Next进程 |
| 上传413/415 | 确认文件≤10 MiB、PDF格式；不要放宽整个BFF |
| 解析失败 | 刷新解析状态，查看安全错误码；加密/损坏/无文本PDF不能靠本界面OCR解决 |
| 问答409 | 核对选中版本；确认解析成功，再显式建立索引 |
| 问答503 | 看Docker容器、Ollama list、模型缓存及处理容量；CPU推理可能较慢，不把错误当作拒答 |
| 504或连接断开 | 没有得到确认；先查看状态并等待，不自动连点重试，不伪造成功 |
| 私有页面不显示 | 先确认/auth/me会话及当前知识库归属；404也可能是无权限 |

浏览器和BFF的同步问答等待上限分别315/300秒，Ollama客户端为240秒；取消等待不等于强制终止模型计算。生产平台/代理的更短时限可能先结束请求。服务日志用于看请求状态及耗时，不记录完整问题/证据、JWT或真实密码。
