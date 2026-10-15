# RAG 模块 HTTP API 契约

版本：1.1-draft · 2026-10-15 · 对应 [多人服务 PRD](PRD_RAG_SERVICE.md)。

**本文件定义待实现的 `/v1` 契约。当前运行验证使用原有 `/api/*`，不能把下面示例视为当前服务已支持。** 一期只开放检索、回读和校验；不需要模型 API Key，不调用内部 Agent。

## 1. 公共约定

- 内网 HTTPS，`Content-Type: application/json; charset=utf-8`；上传单独使用 multipart/form-data。
- `Authorization: Bearer <user-scoped-token>`，服务端确定 principal、角色、工作区/知识库授权。所有下面示例的 ID 都是说明性占位值，不能作为有效授权。
- `X-Request-Id` 可由调用方生成 UUID；服务端校验或生成并回传。`traceparent` 按 W3C 格式接受，身份不从 trace 字段推导。
- `Idempotency-Key`：8–128 字符，仅 ASCII 字母/数字/`-_:.`；同一次传输重试复用，有意的新工具调用换新键。
- `X-Request-Timeout-Ms`：50–2,000；实际 deadline 取客户端请求值与服务配置的较小值，从服务收到请求开始。服务无法按剩余时间执行时尽早拒绝。
- query、文本 budget 和 offset 使用 Python Unicode code point 口径，不是 Token 或字节数。
- 请求 schema 禁止未知字段；响应允许调用方忽略新增可选字段。每次请求独立状态，不把 mutable 数据存到全局会话。

公共成功字段：`schema_version/request_id/elapsed_ms`。错误字段见第 7 节。

## 2. 能力探测

`GET /v1/capabilities`

示例响应：

```json
{
  "schema_version": "1.0",
  "request_id": "req_example_001",
  "capabilities": ["retrieve", "evidence_read", "citation_validate", "pdf_text_ingest_jobs"],
  "model_calls_on_retrieve": 0,
  "limits": {
    "query_chars": 4000,
    "top_k": 20,
    "context_char_budget": 20000,
    "request_timeout_ms": 2000
  }
}
```

只有实际通过验收的功能才进入 capabilities；OCR、公式识别、内部 answer/investigate 默认不列出。客户端在启用相应功能前探测，不能只根据服务版本字符串猜测。

## 3. 检索

`POST /v1/workspaces/{workspace_id}/retrievals`

### 3.1 请求

```json
{
  "query": "随机梯度下降与梯度下降的区别是什么？",
  "knowledge_base_ids": ["kb_shared_books"],
  "top_k": 5,
  "context_char_budget": 12000,
  "filters": {
    "document_ids": [],
    "language": null,
    "tags": [],
    "effective_at": null
  },
  "caller": {
    "agent_run_id": "agent_run_example",
    "tool_call_id": "tool_call_example"
  }
}
```

| 字段 | 规则 |
|---|---|
| query | 必填，去首尾空白后 1–4,000 字符，无 NUL |
| knowledge_base_ids | 必填，1–5 个已授权库；默认无跨库全局搜索；混入一个越权库整体拒绝 |
| top_k | 整数 1–20，默认 5 |
| context_char_budget | 整数 500–20,000，默认 12,000，仅计返回 evidence.text 字符，非模型上下文保证 |
| filters.document_ids | 0–20 个，必须属于选中且授权的库；空列表表示不额外限制 |
| filters.language | null/zh/en；指定时严格匹配，unknown排除并给出过滤警告，不能默默当中文 |
| filters.tags | 0–10 个受支持标签，AND 语义；值大小受限；不接受原始 SQL/表达式 |
| filters.effective_at | null 或 ISO-8601 日期；指定时只返回已确认适用期内资料，未知适用期排除并警告；null不做日期过滤，不从标题猜日期 |
| caller | 可选关联信息，各值≤128字符；不决定身份、授权、计费或配额 |

只检索已发布且当前有效的版本。历史检索后续使用显式接口和单独权限，不能通过 `active=false` 绕过。

### 3.2 响应

```json
{
  "schema_version": "1.0",
  "request_id": "req_example_001",
  "retrieval_id": "ret_example_001",
  "receipt_expires_at": "2026-10-15T09:10:00Z",
  "status": "ok",
  "checked_at": "2026-10-15T09:00:00Z",
  "knowledge_snapshots": [
    {"knowledge_base_id": "kb_shared_books", "generation": 17}
  ],
  "retrieval": {"strategy": "bm25", "version": "bm25-v1", "score_type": "bm25"},
  "evidence": [
    {
      "evidence_id": "ev_example_001",
      "knowledge_base_id": "kb_shared_books",
      "document_id": "book_example_001",
      "revision_id": "rev_example_002",
      "title": "书籍标题",
      "text": "片段正文，由检索服务提供。",
      "score": 12.5,
      "locator": {
        "kind": "pdf",
        "pdf_page_start": 266,
        "pdf_page_end": 266,
        "printed_page": null,
        "chapter_path": [],
        "text_revision": "text_example_002",
        "start_offset": 0,
        "end_offset": 13
      },
      "text_sha256": "c18bc7338d36f7c2e6e774173e4bb1258f5fd03dcdd389409644da98ad7bfc16",
      "quality_flags": [],
      "untrusted_content": true
    }
  ],
  "returned_chars": 13,
  "truncated": false,
  "warnings": [],
  "usage": {"model_calls": 0, "tool_calls": 1},
  "elapsed_ms": 48
}
```

示例的正文长度、offset 与哈希已核对一致，但 ID 和时间仍为说明值，并非服务器上的有效证据。最终机器契约落地时应由同一 fixture 自动生成这些关联字段。

实现规则：

- 每个 evidence 是一个不可变文本视图，`text == referenced_text[start_offset:end_offset]`，`text_sha256` 为实际 UTF-8 文本摘要。预算截取要相应调整偏移和摘要，不能仍声称是完整片段。
- 排名分数只用于本次 BM25 排序，不是置信度；不同策略/语料/查询不可直接横比。
- 多知识库按授权候选统一排名或经明确校准合并；策略版本必须表达该行为，不能把原始跨库分数直接冒称可比。
- 返回少于 top_k 可能因为不足、去重或正文预算；`truncated` 只表示预算裁剪，不表示回答不完整或资料不存在。
- 没有匹配时 `status=empty,evidence=[],returned_chars=0`，仍返回 receipt/知识快照；不把它解释为已证明问题不可回答。
- 返回时复查来源与授权，校验与 receipt 保存处于同一短事务；generation 改变导致证据失效则 409，不能以旧 active 标记完成。
- 服务端不返回原始磁盘路径；UI 通过受权下载/页面接口打开原文。

### 3.3 receipt 的含义

retrieval_id 是服务端持久 receipt 的索引，不是绕过授权的能力令牌。记录 principal、工作区、知识快照、取得的证据集合、字段视图哈希和过期时间，保留 10 分钟。

同一次外层 Agent 任务可以取得多份 receipt；最终校验最多携带 6 份，服务核对全部属于当前 principal/工作区。不能提交未经本次工具取得的任意 ID，不能拿另一用户 receipt 拼接引用。

传输重试的幂等执行结果在有效期内可重放，但每次发送正文前重新授权/查版本；知识变化返回 KNOWLEDGE_CHANGED，超期返回 RECEIPT_EXPIRED。

`GET /v1/workspaces/{workspace_id}/retrievals/{retrieval_id}` 提供幂等结果状态读取。服务在领取幂等执行记录时即分配retrieval_id，处理中重放用Location给出此URL。授权且仍有效时，完成返回原检索响应；处理中返回202/processing与Retry-After；过期410，越权403，不暴露其他人的记录是否存在。读取也计用户配额，并不因持有ID免鉴权。

## 4. 证据回读与最终引用校验

### 4.1 回读

`POST /v1/workspaces/{workspace_id}/evidence:read`

```json
{
  "retrieval_ids": ["ret_example_001"],
  "evidence_ids": ["ev_example_001"],
  "context_char_budget": 12000
}
```

最多 6 份 receipt、20 个唯一 evidence_id，正文预算同检索。回读只允许这些 receipt 已取得的证据；当前 source/ACL 不合法则整体失败，不以部分成功掩盖越权。返回与 retrieval 相同 evidence 结构，并创建独立补充receipt，绑定本次实际返回的文本视图；旧receipt不可变，调用方把新retrieval_id加入最终验证集合。

一期回读的粒度为原片段及其合法完整文本，不允许任意跨章节扩展。未来 Parent-Child 阅读需明确父证据如何成为已取得证据，不能只验证 ID 而模型没有看到对应正文。

### 4.2 最终验证

`POST /v1/workspaces/{workspace_id}/citations:validate`

```json
{
  "retrieval_ids": ["ret_example_001"],
  "citations": [
    {"evidence_id": "ev_example_001", "text_sha256": "c18bc7338d36f7c2e6e774173e4bb1258f5fd03dcdd389409644da98ad7bfc16"}
  ]
}
```

最多 30 个唯一引用。只有所有引用均属于本次取得集合、文本视图哈希正确、当前版本有效且权限通过，才返回：

```json
{
  "schema_version": "1.0",
  "request_id": "req_validate_example",
  "valid": true,
  "checked_at": "2026-10-15T09:00:05Z",
  "knowledge_snapshots": [{"knowledge_base_id": "kb_shared_books", "generation": 17}],
  "semantic_support": "not_checked",
  "elapsed_ms": 9
}
```

有效性仅覆盖 checked_at 的事务检查点；调用方发送答案后原文仍可能更新。Agent 应保留引用版本，后续打开历史回答时显示当前有效性，不把验证说成“回答零幻觉”。没有引用的回复不应调用此接口来获得可展示的“全部验证通过”徽标。

错误：过期 receipt 410；未取得证据/用户不符 403；正文哈希不符或来源更新 409；找不到授权资源 404；服务故障 503/504。整批失败，公共响应不暴露越权对象是否存在。

## 5. 长任务与条件发布

### 5.1 上传

`POST /v1/workspaces/{workspace_id}/uploads`，multipart字段：`file,knowledge_base_id,mode,logical_source_key,document_id,expected_revision_id`。mode必填new/update；new禁止携带更新目标，update必须给document_id与expected_revision_id，logical_source_key由目标记录核对而不能跨书覆盖。响应202：

```json
{
  "schema_version": "1.0",
  "request_id": "req_upload_example",
  "job_id": "job_example",
  "status": "queued",
  "status_url": "/v1/workspaces/ws_example/jobs/job_example",
  "elapsed_ms": 320
}
```

`Location` 指向 status_url。上传鉴权在接收大正文前完成，边流边限制 200MiB；反向代理限制与应用限制一致。文本请求上限保持独立，不将所有 JSON 允许到 200MiB。

### 5.2 状态与取消

`GET .../jobs/{job_id}` 返回 `status/pages_total/pages_done/quality_summary/attempt/updated_at/result/error`，未知总数时 pages_total=null；不把页数比例伪装成端到端精确百分比或 ETA。

`POST .../jobs/{job_id}:cancel` 表达持久取消请求；接受后 202/cancel_requested，只有 worker 确实停止才 cancelled。已经 ready/failed/cancelled 返回终态，重复取消幂等。客户端断开上传只取消尚未完成接收；已返回202的job默认继续，须调用cancel才停止。

### 5.3 发布

`POST .../documents/{book_id}/revisions/{revision_id}:publish`，必填 Idempotency-Key 和 `If-Match: "<current-revision-id>"`；新增书籍用显式 `If-None-Match: *`。

发布要求 publisher 权限、质量复核完成、索引已准备、预期当前版本未变化。数据库原子切换当前指针、generation、Wiki失效及审计；预条件缺失428，预条件失败412，业务修订状态冲突409。公开API应统一用条件请求；现有expected_version接口迁移适配后不得混淆HTTP码。

ready 表示已确认检索实例加载新代次，之前 publishing 可由任务状态查询；不得在未就绪时返回 ready。发布操作的幂等键保留24h，网络结果未知时查询job与revision状态。

## 6. Python / LangGraph 接入约定

### 6.1 连接与工具边界

Python客户端持有可复用AsyncClient，关闭时释放连接。汇聚20用户的调用服务初值 `max_connections=20,max_keepalive_connections=20`，同用户tool调用由独立信号量串行化；单用户独立客户端可更小。不能把一个全局4连接池的等待混算成RAG服务器容量不足。每次调用均将用户限定令牌放入独立headers，不能在共享client上并发修改默认Authorization；token不放进模型可见tool schema。

LangGraph工具仅暴露 `query/top_k/filters` 等有限参数。workspace、可访问knowledge_base_ids、token与用户任务budget由调用方依赖注入；工具不能接受模型指定的URL、文件路径或任意HTTP方法。工具结果附 `retrieval_id/evidence_id/version/locator/hash/quality_flags`，内容作为不可信证据。

模型上下文不足时由外层Agent选证据，不能删掉来源元数据却保留结论。工具的timeout/overload异常以明确结构返回，使外层可以停止或有界重试，不返回“没有知识”。

### 6.2 建议顺序

1. 验证 capabilities 与配置的知识库授权。
2. 发起 retrieve，生成唯一工具调用幂等键，记录 receipt。
3. 需要时 read 取得原文；所有调用共享外层总budget，Agent也可能决定再次retrieve。
4. 外层模型依据证据生成回答，引用限制在实际返回集合。
5. citations:validate；409时最多重新取证一次，403/410不能带旧引用继续。
6. 返回答案并保留来源版本；日志只留公开运行关联字段。

本轮不引入LangGraph运行依赖或真实模型；未来集成验收需覆盖真实节点取消、并行tool调用的预算共享和跨用户隔离。只使用HTTP测试替身无法证明这一层集成已经完成。

### 6.3 deadline 与重试

客户端必须施加端到端3s截止，服务端最多2s。`httpx.Timeout` 的 connect/read/write/pool 为分阶段限制，需再用 `asyncio.wait_for` 等总时限约束；慢速连续字节不能无限延长任务。

只对确认未发送的连接失败或允许重试的429/503，在剩余总budget内最多重试1次。Retry-After超出剩余budget时直接停止；处理中 REQUEST_IN_PROGRESS 只允许按明确Location轮询状态或交由调用方等候，不对所有409自动重放。上传/发布超时后先查状态，再以原幂等键恢复，禁止盲目重做。

## 7. 错误模型

```json
{
  "schema_version": "1.0",
  "request_id": "req_example_error",
  "error": {
    "code": "OVERLOADED",
    "message": "检索容量暂满，请稍后重试。",
    "retryable": true,
    "retry_after_ms": 500
  }
}
```

| HTTP | code | 调用方行为 |
|---|---|---|
| 400 | BAD_REQUEST | 修正字段/格式，不重试 |
| 401 | UNAUTHENTICATED | 重新认证/轮换令牌，禁止无限刷新 |
| 403 | PERMISSION_DENIED / EVIDENCE_NOT_OBTAINED | 停止，不泄漏其他对象信息 |
| 404 | RESOURCE_NOT_FOUND | 停止或刷新授权资源目录 |
| 409 | KNOWLEDGE_CHANGED / HASH_MISMATCH | 明确重新取证；不重放旧结果 |
| 409 | IDEMPOTENCY_CONFLICT | 同键异体，修复客户端 |
| 409 | REQUEST_IN_PROGRESS | 按Location状态查询；非通用重试授权 |
| 410 | RECEIPT_EXPIRED | 新检索，不能继续使用旧receipt认证引用 |
| 412 / 428 | PRECONDITION_FAILED / PRECONDITION_REQUIRED | 刷新编辑基础或补条件头 |
| 413 | PAYLOAD_TOO_LARGE | 拆分/减少文件，不能自动放大限制 |
| 422 | VALIDATION_ERROR / PDF_NEEDS_REVIEW | 修正输入或人工复核 |
| 429 | USER_RATE_LIMIT / USER_CONCURRENCY_LIMIT | 按Retry-After及剩余deadline处理 |
| 503 | OVERLOADED / INDEX_NOT_READY / STORAGE_UNAVAILABLE | 可有限重试；不等同空结果 |
| 504 | DEADLINE_EXCEEDED | 结束本次工具调用，按错误阶段处理未知结果 |

502/UPSTREAM_UNAVAILABLE只用于后续启用的外部模型/解析能力；一期纯检索不应制造供应商依赖。公共响应不包含敏感异常文本；服务内部记录脱敏分类与request_id。

## 7.1 维护与身份入口清单

团队版还需下列接口（M2交付各自schema和契约测试），通过同一个鉴权中间层保护新旧路径：

| 接口 | 语义与最低约束 |
|---|---|
| `POST /v1/sessions`、`DELETE /v1/sessions/current` | 登录/登出；失败限速、会话可撤销、无默认密码 |
| `GET /v1/me` | 当前principal/角色/scope，不能查看密钥 |
| `POST /v1/tokens`、`DELETE /v1/tokens/{id}` | 用户申请不超过自身权限的短期只读令牌/撤销；密钥仅显示一次 |
| `GET /v1/workspaces/{wid}/knowledge-bases` | 只返回授权知识库，无全局未授权目录 |
| `GET .../documents?cursor=...&limit=...` | 授权资料分页；limit默认20、上限100，游标绑定排序/查询条件 |
| `GET .../documents/{book_id}/revisions/{rid}` | 修订详情与质量报告；历史访问仍需权限 |
| `GET .../documents/{book_id}/revisions/{rid}/pages/{page}` | 受权页面内容/预览，不暴露本机绝对路径 |
| `POST .../wiki/drafts`、`POST .../wiki/{id}:publish` | 编辑/发布分权；条件更新与底层来源同事务复查 |
| `GET .../audit-events` | 管理员分页审计，普通reader不可列出他人查询 |
| 管理用户/知识库授权、管理员备份恢复入口 | 仅admin；细化schema须在对应功能实现前评审，不能复用reader服务令牌 |

文档/原文件下载需独立read/download scope，受响应大小、带宽和并发限制；可采用短期、身份绑定下载句柄，不能签发无限期公开链接。`/api/examples`等旧seed入口在团队部署关闭，不能被Agent调用。

## 8. 事件与状态读取

短检索默认JSON，避免每次tool都建立长SSE。导入默认轮询，建议1s起步退避到5s，有总等待期限；不要每100ms刷状态。

后续调查接口若使用SSE，event_id单调、公开事件持久化，Last-Event-ID可在保留期内重放，保留1小时；过期返回410并指引读取最终状态。每连接队列最多64事件，慢读超过15s关闭连接，不阻塞业务worker无限写队列。Bearer鉴权用fetch流或服务端SDK；浏览器原生EventSource不能自带任意Authorization头，不能把token塞URL解决。

## 9. API验收清单

- 请求与响应JSON Schema、实际OpenAPI在M2交付，字段/fixture由契约测试约束。本文件提供产品语义，不冒充已生成可执行OpenAPI。
- 双用户相同query、跨库ID、过期receipt、撤销token、并发共享client headers必须有隔离测试。
- 覆盖成功/空结果/拒绝/超时/索引更新/上传断线/同键重放/同键异体/重启恢复。
- 性能验收明确JSON正文、片段长度、冷暖状态与ID解析成本，不能把健康检查QPS当检索容量。
- 容量内验收为20个授权身份各1个同时请求全部按时成功，0个429/503/504；再测20个Agent连续retrieve/read/validate，确保同用户串行、跨用户并发、连接池和全局突发额度不会误拒绝后续步骤。超过容量的拒绝测试独立报告。
- 本轮旧API全书运行报告单独标注compatibility_mode=legacy_api；禁止为凑“跑通”结果把旧/api/query返回值改名成/v1/retrievals响应。
