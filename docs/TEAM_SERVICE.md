# 单机团队 RAG 服务：部署与 Agent 接入

日期：2026-10-16。适用范围：内部 10–20 人、一个 workspace、多知识库、一个 API 进程与独立导入 worker。服务默认不调用模型。容量与书籍运行证据见 [验收记录](TEAM_ACCEPTANCE.md)，产品语义见 [PRD](PRD_RAG_SERVICE.md) 与 [API 契约](RAG_API_CONTRACT.md)。

## 1. 两个入口与数据边界

| 入口 | 用途 | 默认目录 | 认证 |
|---|---|---|---|
| `python -m zhiwiki` | 原本地工作台、内部 Agent 调查、SSE | `data/` | 本机使用，不作为团队入口 |
| `python -m zhiwiki.service` | 外部 Agent 的证据服务、原生 PDF 任务、维护 API | `data-team/` | 会话或用户限定 Bearer 令牌 |

团队服务关闭 `/api/*`，未认证请求 401，已认证访问旧路由 410。原工作台应继续只绑定回环地址；不要把旧端口作为团队服务的反向代理目标。团队登录界面提供基本检索，文档维护与授权通过 API 操作。

原文事实仍在 `documents/chunks`，Wiki 在 `wikis/wiki_sources`。`team_*` 表增加账号、授权、整书修订、页映射、任务、receipt、幂等与审计，不另写一套原文库。

迁移原本地库时先停止旧服务，完成迁移后通过团队入口维护该目录；不支持旧工作台与团队服务同时写同一数据目录。默认的两个独立目录可以分别运行。

## 2. 安装与首次启动

完整服务使用 Python 3.13，固定依赖见 `requirements.txt`；PDF 子进程需 `requirements-parser.txt`。LangGraph 是调用方集成依赖，不是服务启动条件。

```bash
python -m pip install -r requirements.txt -r requirements-parser.txt
python -B -m zhiwiki.service --config config.team.example.json bootstrap admin
```

首次管理员密码交互输入，至少 12 字符。无人值守时可通过 `bootstrap admin --password-env ZHIWIKI_BOOTSTRAP_PASSWORD` 读取明确命名的**运行时进程变量**，不读 `.env`。已有管理员后 bootstrap 拒绝重复创建，后续账号操作需登录管理员 API。

两个终端分别启动：

```bash
python -B -m zhiwiki.service --config config.team.example.json serve --port 8872 --local-http
python -B -m zhiwiki.service --config config.team.example.json worker
```

`--local-http` 仅用于回环演示，允许浏览器发送非 Secure cookie。正常部署保留 `cookie_secure: true` 并移除此参数。API 默认监听 `127.0.0.1`；浏览器入口为 [本机 8872](http://127.0.0.1:8872)。

需要独立解析环境时，在显式 JSON 配置中添加 `worker_python` 的绝对路径，该 Python 必须能导入 `pypdf==6.10.0`。worker supervisor 在主服务环境运行，子进程用配置的解释器；仓库根目录作为子进程工作目录。配置文件中的相对 `data_dir/worker_python` 以配置所在目录解析。

## 3. 创建知识库和用户

下面代码使用当前进程内存保存密码和 cookie，不输出令牌。应在可信管理终端执行；没有预设示例账号密码。

```python
import getpass
import httpx

base = "http://127.0.0.1:8872"
with httpx.Client(base_url=base, trust_env=False, timeout=3) as admin:
    login = admin.post("/v1/sessions", json={
        "username": "admin", "password": getpass.getpass("管理员密码: ")
    })
    login.raise_for_status()
    admin.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    kb = admin.post("/v1/workspaces/default/knowledge-bases", json={"title": "共享资料"})
    kb.raise_for_status()
    kb_id = kb.json()["knowledge_base_id"]
    created = admin.post("/v1/users", json={
        "username": "reader01", "password": getpass.getpass("新用户密码: ")
    })
    created.raise_for_status()
    user_id = created.json()["user_id"]
    granted = admin.put(
        f"/v1/workspaces/default/knowledge-bases/{kb_id}/members/{user_id}",
        json={"role": "reader"}
    )
    granted.raise_for_status()
    print("知识库ID:", kb_id, "用户ID:", user_id)
```

角色 reader 可检索与回读；editor 可上传、校订未发布页与维护 Wiki 草稿；publisher 可复核/发布；系统管理员可管理账号、知识库授权与审计。库级 `admin` 角色不会把账号提升为系统管理员。授予更高角色时沿用成员 PUT 接口，撤销用同路径 DELETE。

账号密码使用带随机盐的 scrypt，令牌随机生成、仅存 SHA-256 哈希。浏览器会话 8 小时，PAT 最多 7 天。会话写操作须 `X-CSRF-Token`，cookie 为 HttpOnly/SameSite Strict，跨来源 Origin 拒绝；Bearer 不使用 cookie CSRF，但仍接受来源检查。

用户在自己的登录会话下调用：

```http
POST /v1/tokens
X-CSRF-Token: <登录返回值>
Content-Type: application/json

{"knowledge_base_ids":["kb_..."],"scopes":["retrieve","read","validate"],"expires_in":604800}
```

响应 `token` 只显示一次。调用方将其放入进程运行环境；不要交给模型、放入 URL、代码或公开报告。撤销使用 `DELETE /v1/tokens/{token_id}`；禁用用户使用管理员 `POST /v1/workspaces/default/users/{user_id}:disable`。令牌权限与当前库授权取交集，每次查询及最终 receipt 提交前重新验证。

## 4. 原生导入与整书发布

```http
POST /v1/workspaces/default/uploads?knowledge_base_id=kb_...
Cookie: <登录会话>
X-CSRF-Token: <csrf>
Idempotency-Key: import-<唯一逻辑调用ID>
Content-Type: multipart/form-data
```

multipart 字段为 `knowledge_base_id`、`mode=new`、`logical_source_key`、`file`。URL 与表单中的库 ID 必须一致：URL 用于在接收大正文之前检查目标库授权。禁止路径文件名、`.env` 文件和模型传入的任意本机路径。

新增书籍必须显式给稳定的逻辑来源键；更新时设置 `mode=update`，补充 `document_id` 和 `expected_revision_id`。旧版不再是当前版时 409；同来源、同文件哈希、同 pipeline 可复用已有任务，不按文件名猜测更新对象。

返回 202，正文含 `job_id/status_url`，Location 指向任务状态。约每 1 秒轮询，必要时退避至 5 秒。202 后任务独立于 HTTP 连接继续执行；取消须显式调用 `POST .../jobs/{id}:cancel`。

状态主线为 `queued → parsing → needs_review/awaiting_publish → publishing → ready`；终态还包括 failed/cancelled。页比例仅表示解析进度，不是剩余时间估计。worker 只能持有效租约/fence 提交页，最多一个解析任务运行、10 个任务等待；失败或取消后可 `POST .../jobs/{id}:retry`，保留已提交页，最多领取 3 次。

PDF 每页分别保存 raw_text、NFKC 规范化文本、物理 PDF 页码及质量信息。NUL 缺字标记显示为 U+FFFD，不静默抹去；空页和低文本页标记待复核。页面接口：

```http
GET .../documents/{book}/revisions/{revision}/pages/{page}
PUT .../documents/{book}/revisions/{revision}/pages/{page}
```

校订 PUT 需 editor，正文包含 `text/expected_text_sha256/note`。仅待审修订可编辑，保留原抽取文本；条件冲突 412，编辑后重新复核，已发布页不可原地修改。

publisher 用 `POST .../documents/{book}/revisions/{revision}:review` 提交至少 5 字符的复核说明，再发布：

```http
POST .../documents/{book}/revisions/{revision}:publish
Idempotency-Key: publish-<唯一逻辑调用ID>
If-None-Match: *
```

新增书用 `If-None-Match: *`；更新已有书改用 `If-Match: "<当前revision_id>"`。预条件缺失 428，冲突 412。索引先构建、校验和原子命名，再用短事务切换整本书、知识代次、旧来源失效及审计；API 加载新代次后才标 ready。准备失败不影响旧版服务，未发布页不会混入检索。

扫描件、加密 PDF、缺字与公式复杂页不能靠“任务完成”证明识别准确。全空书籍禁止发布，返回 PDF_NEEDS_OCR；本轮不下载模型、不做 OCR 或公式结构还原。

## 5. Agent 调用

长期复用 `RagClient`，每实例一个身份；可注入同一长期 HTTPX 客户端供多身份共用连接池。每请求单独携带 Authorization，禁止并发修改共享默认 headers。

```python
import asyncio
import os
from zhiwiki.service.client import RagClient

async def run():
    async with RagClient("http://127.0.0.1:8872", os.environ["ZHIWIKI_TOKEN"]) as rag:
        result = await rag.retrieve("随机梯度下降是什么？", ["你的库ID"])
        if not result["evidence"]:
            return result  # 正常 empty，不伪造答案。
        read = await rag.read(
            [result["retrieval_id"]], [item["evidence_id"] for item in result["evidence"]]
        )
        checked = await rag.validate([read["retrieval_id"]], [
            {"evidence_id": item["evidence_id"], "text_sha256": item["text_sha256"]}
            for item in read["evidence"]
        ])
        return {"evidence": read["evidence"], "validation": checked}

asyncio.run(run())
```

外层 Agent 自行规划和生成答案，最终回答前再次验证实际使用的引用；RAG 不再次调用内部 Agent。校验只保证身份、来源版本、回读集合和文本哈希，`semantic_support=not_checked`，不保证结论与原文的语义蕴含。

LangGraph 节点通过依赖注入冻结库与令牌：

```python
from langgraph.graph import StateGraph, START, END
from zhiwiki.service.client import langgraph_node
from typing import TypedDict, NotRequired

class State(TypedDict):
    query: str
    rag: dict
    rag_deadline: NotRequired[float]

# rag_client 是上文已打开的长期 RagClient，kb_id 来自授权知识库。
graph = StateGraph(State)
graph.add_node("evidence", langgraph_node(rag_client, [kb_id], task_timeout=9))
graph.add_edge(START, "evidence")
graph.add_edge("evidence", END)
agent_graph = graph.compile()
result = await agent_graph.ainvoke({"query": "退货期限如何确定？"})
```

节点是检索/回读/校验三步，不含 LLM。调用方可通过 `rag_deadline` 注入更早的 monotonic 总截止；每个 HTTP 调用仍最多 3 秒。节点取消传递到 HTTP 层，服务端线程真正结束前不归还运行槽。

同一次工具调用重试复用幂等键；连接建立失败、允许重试的 429/503 最多重试一次，并计入总 deadline。401/403/409/410/422 不自动重放，504/读超时属于未知结果，不能直接换键重复执行。查询状态可用 `GET .../retrievals/{receipt_id}`；同键运行中 409 含 Location。

状态GET使用独立的小门禁（全局4个、每身份1个），共享速率预算，不与原工具争用同一身份在途位。原任务运行时重复POST仍可能先触发429，此时按已有Location查询状态。查询别人的receipt仍被拒绝。

## 6. 并发预算与扩容条件

| 预算 | 默认值 | 用途 |
|---|---:|---|
| 执行 / 接纳 | 4 / 20 | 其余最多 16 个 FIFO 等待，超全局接纳 503 |
| 每身份在途 | 1 | 同用户抢占返回 429，不靠每个 API key 分开计数 |
| 排队 / 总时限 | 1,500 / 2,000 ms | 排队和 DB 检查均包含在同一服务截止中 |
| 用户速率 / 突发 | 30 rpm / 6 | 覆盖任务内多步工具调用 |
| 全局速率 / 突发 | 120 rpm / 120 | 20×6 次调用可突发，不能持续无限重复 |
| 上传并发 | 全局 2、每用户 1 | 先鉴权、流式大小与总时限检查 |
| 下载并发 | 全局 4、每用户 1 | 单次最多 180 秒，单次发送等待最多 10 秒 |
| 解析进程 | 1 | 默认 1 GiB OS 内存限制、每页进展 30 秒、单任务 30 分钟 |

“20 人在线”不等于“持续每秒 20 次检索”。30 分钟容量口径是总 1 req/s，另测 20 身份同步突发及工具链。达到外部模型阶段时需另设模型 RPM/TPM、连接池、成本与熔断，本轮查询没有这些出站请求。

禁止直接增加 `uvicorn --workers`；内存信号量会复制。需要多 API/多机时先迁出全局门禁、限流、幂等协调和持久队列，再依据实测评估 PostgreSQL/Redis/专用检索服务。当前没有 HA 保证。

## 7. TLS、进程和磁盘

复制 JSON 模板为 `config.team.local.json`，设置实际 `allowed_hosts`，不接受通配 Host。应用仅信任回环反代的转发头；反向代理必须覆盖客户端提供的 X-Forwarded-For。TLS 域名/证书需部署管理员提供，本仓库没有替你申请或发布公网服务。

同机 Nginx 关键配置示例：

```nginx
server {
    listen 443 ssl;
    server_name rag.internal.example;
    ssl_certificate /etc/nginx/tls/rag.crt;
    ssl_certificate_key /etc/nginx/tls/rag.key;
    client_max_body_size 201m; # 200 MiB 文件，加 multipart 传输开销
    client_body_timeout 30s;
    location / {
        proxy_pass http://127.0.0.1:8872;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_request_buffering off;
        proxy_read_timeout 190s;
    }
}
```

应用对检索单独执行 2 秒截止；上传最多 180 秒。大文件下载的网速配额由代理配置，例如对 file 路径设置 `limit_rate 2m`。使用操作系统服务管理器分别监督 API 与 worker，异常重启；运行账号仅有代码读取与数据目录写入权限。不要把 data、备份或配置目录映射成静态网站。

每库最多 50,000 片段，单索引文件 512 MiB，数据目录默认 10 GiB 准入水位，保留至少 512 MiB 空闲空间。准入检查不是操作系统硬磁盘配额；多项操作同时写入时仍需文件系统配额和监控。历史文件不会因“清理”自动删除，增长到水位后拒绝新写入，需维护者扩容或制定保留策略。

## 8. 指标、日志与恢复

管理员 `GET /v1/workspaces/default/metrics` 返回请求计数、最近最多 2,048 样本的 P50/P95/P99、队列、活动槽、超时后仍运行数量、SQLite busy/写等待、RSS/Python 线程/空闲磁盘、任务状态及 worker 心跳。`/health/live` 检查事件循环，`/health/ready` 检查 DB 写事务、磁盘和已加载代次；worker 心跳独立报告，不让 worker 停止拖垮已发布资料的纯检索。

请求日志保留 request_id、有效 traceparent、principal、操作、状态和耗时，不记录查询、正文、密码或令牌。审计存于数据库，管理员分页读取 `/audit-events`。指标是进程内窗口，重启归零；审计和任务状态持久保存。

```bash
# 必须选择新的空目录，不能只复制活跃 knowledge.sqlite3 主文件。
python -B -m zhiwiki.service --config config.team.local.json backup data-team-snapshot-20261016

# 验证清单/哈希/数据库完整性后恢复到另一个新的空目录。
python -B -m zhiwiki.service restore data-team-snapshot-20261016 data-team-restored
```

备份使用 SQLite backup API，按数据库快照复制对应原文件与不可变索引，再生成 SHA-256 manifest。包含账号、授权和资料，须限制访问；不包含模型密钥、运行时进程变量、`.env` 或不相关本机文件。

恢复后把配置指向新目录，先在回环测试端口启动、检查 ready、授权和检索，再由管理员切换代理目标。旧目录和快照保留，恢复不覆盖现有目录。首次打开旧本地库前会自动生成一致的迁移前快照，旧 ID 不变，资料映射进默认无普通成员授权的 `kb_legacy`。

每分钟自动清理已过期 receipt/幂等记录；也可运行 `cleanup`。原文件、版本、Wiki、审计与用户数据不自动删除。管理员尚需指定受控备份位置与调度；RPO≤24h/RTO≤2h 属部署目标，不能把一次本机恢复耗时当作已实现的灾备保证。
