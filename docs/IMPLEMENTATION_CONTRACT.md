# 实现接口约定

此文件在 PRD 落盘后固定，用于模块并行实现。所有代码 Python 3.9 可解析，注释清楚、小函数；不读取 `.env`，不安装包。包名 `zhiwiki`，导入无数据库/网络副作用。

## 存储接口：`zhiwiki.store.KnowledgeStore(data_dir)`

字典只含 JSON 可序列化值。UUID 类 ID 由服务生成；类型前缀区分 chunk / wiki / doc / run。异常 `ValueError` 表示无效输入或失效状态，`KeyError` 表示不存在。

- `ingest(title, text, source_key) -> document`：文档含 `id, source_key, title, version, content_hash, active, created_at, chunk_ids, unchanged`。
- `list_documents(include_history=False) -> list[document]`。
- `get_document(document_id) -> document`：额外含 `text, chunks`。
- `search(query, kind='document', limit=5) -> list[evidence]`：`kind` 为 document/wiki/all；BM25；只返回当前文档和最新有效已发布 Wiki。
- `read_evidence(evidence_id) -> evidence`：历史可读，`active` 显示是否仍可作为当前证据。
- evidence 字段：`id, kind, title, text, source_key, version, locator, document_id, active, score`；Wiki 额外 `source_ids`，为底层 chunk ID。
- `create_wiki(slug, title, claims, mode='offline', links=None) -> wiki`：claims 为 `[{text: str, evidence_ids: list[str]}]`，每条必须有当前原文 chunk 来源，拒绝 Wiki 自引。写入不可变修订，初始 draft。Wiki 含 `id, slug, title, version, status, stale, claims, source_ids, body, links, mode, created_at`。
- `get_wiki(wiki_id) -> wiki`。
- `list_wiki(include_history=False) -> list[wiki]`：默认每个 slug 最新修订。
- `publish_wiki(wiki_id) -> wiki`：事务内核对 active 原文；发布新版本后旧发布版本转 archived；不得发布已被较新版本替代的修订。
- `lint_wiki() -> list[{id, code, message}]`：失效/缺引/草稿/坏链接等确定性检查。
- `export_wiki(wiki_id) -> str`：Markdown，包含每条 claim 与底层来源标题/版本/定位。
- `save_run(payload) -> str`：payload 已含 id 时保留（run_ UUID），补写入时间。
- `get_run(run_id) -> dict`。

`zhiwiki.ingest.read_file(path)` -> `{title, text, source_key}`；仅 md/txt/docx，拒绝任一路径组件 `.env*`，解析安全与长度限制。`parse_upload(filename, data)` -> 同形字典，不在磁盘写上传文件。文本上限 500,000 字符，单文件 2 MiB，DOCX 展开总量 10 MiB。

## 设置与模型接口：`zhiwiki.config` / `zhiwiki.provider`

`Settings` dataclass：`data_dir='data', provider='offline', base_url='', model='', api_key_env='RAG_API_KEY', max_rounds=6, max_tool_calls=12, timeout_seconds=60.0, request_timeout_seconds=20.0, max_context_chars=24000, max_total_tokens=24000, max_output_tokens=1500`。

`load_settings(path=None, **overrides) -> Settings`：只读显式 JSON 配置文件，拒绝 `.env*`，未知字段与越界值拒绝；相对 data_dir 以配置文件目录为基准（无配置则 cwd）。配置不允许 api_key 字段；密钥只在 provider 使用时从指定运行时环境变量取。

`OpenAIProvider(settings, client=None)`：httpx 兼容，可注入 MockTransport client；`complete(messages, tools=None, timeout=None, max_tokens=None) -> {message: dict, usage: {prompt_tokens, completion_tokens,total_tokens}}`。消息符合 Chat Completions，工具使用标准 function calling。无密钥仅允许 loopback 服务。远程必须 HTTPS。不回显外部响应正文、密钥或请求头；超时/响应失败 -> `ProviderError`。无静默 offline 降级。

## 执行接口：`zhiwiki.engine`

`AgentEngine(store, settings, provider=None)`；未注入 provider 时根据 settings 创建 OpenAIProvider；offline 不建立连接。

- `run(question, strategy='agentic', on_event=None) -> result`：strategy baseline/agentic；question 非空且 <=4000 字符；on_event 收到 `{event: str, data: dict}`，实时回调。
- result：`id, question, strategy, mode, status, answer, citations, events, usage, elapsed_ms, stop_reason`。
- status：completed / insufficient_evidence / budget_exceeded / provider_error / invalid_answer。
- citations 为 evidence 字典列表，只能引用本次取回的且 active 的证据；Wiki 必须附底层来源且校验 active。
- usage 至少含 `model_calls, tool_calls, prompt_tokens, completion_tokens, total_tokens, token_usage_known`；缺失 usage 不当作真实 0，用输入/输出保守估算预算，明确标识。
- 工具：`search_documents(query, limit=5)`，`search_wiki(query, limit=5)`，`read_evidence(evidence_id)`。
- 工具 trace 对外仅公开选择、参数和结果摘要，不记录隐藏思维。
- 模型最终输出 JSON：`{"answer":"...", "evidence_ids":["chunk_..."], "insufficient_evidence":false}`。格式与引用不合要求明确返回 invalid_answer。
- offline 为确定性检索摘录，标记 mode=offline / stop_reason=offline_extract；不得视为 LLM 自主多步结果。
- run 最终保存 store，on_event 最后发送 final。

## Wiki 生成：`zhiwiki.compiler.WikiCompiler(store, settings, provider=None)`

`draft(slug, title, document_ids, on_event=None) -> wiki`：原文分块必须当前有效；限制所选资料数量和上下文。真实模式模型输出 `{claims:[{text,evidence_ids}],links:[]}`，验证只能引用本次供应的原始 chunk；离线建立原文摘录草稿。任何模式均只创建 draft，不自动发布。复用 provider/config，与 Agent 分开预算。返回 wiki 包含 mode。

## 职责边界

- 存储代理拥有 store.py、retrieval.py、ingest.py、tests/test_store.py、tests/test_ingest.py。
- 执行代理拥有 config.py、provider.py、engine.py、compiler.py、tests/test_engine.py、tests/test_provider.py、tests/test_compiler.py。
- 主代理拥有 HTTP/CLI、样例、评测、README、装配与集成测试。
- 审查代理只读审查与独立验收建议，不覆盖其他代理代码。
