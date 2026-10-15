# 策略依据、实现映射与差距

核查日期：2026-09-09。核查方式：实际浏览维护方文档、官方 GitHub 源码，并对照本地 Python 实现。本页没有安装或下载任何库、模型或包。

## 1. 如何使用这份依据

这里的“一手来源”包括云厂商产品文档、厂商工程文章、项目维护者的代码与教程。**官方教程和开源实现能够证明一种设计确实存在，不能证明本项目已经达到生产规模、已经复现其效果，或获得厂商背书。** GitHub 链接指向核查当日的 `main` / `master`，并非固定提交；复用源码前应另行固定版本、检查许可证和运行兼容性。

下文明确区分三类内容：

- **来源事实**：浏览到的官方文档或源码直接支持的行为。
- **本项目映射**：核查时本地代码已经实现的对应部分。
- **差距与设计推论**：尚未实现、需要验证，或是依据原则提出的本地设计选择。

**900 字符 / 120 字符重叠是本项目的初始对照参数，不是官方推荐值，也没有经过业务效果调优。** 字符是 Python Unicode code point，不是 Token。不能将官方文档中的 Token 参数直接等同于本项目字符参数。

## 2. 文档切分与上下文组织

### S01 · Azure AI Search：根据内容结构与任务选择切分方式

来源：[Chunk large documents for RAG and vector search in Azure AI Search](https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-chunk-documents)。类型：微软产品文档。

- **来源事实**：文档区分固定大小、按句段或标记结构的变长切分，以及语义切分；指出标题等上下文可辅助解释片段，重叠量应结合内容和用途调整。
- **技术启示**：块大小、边界与重叠要共同设计，并通过实际查询验证；一个固定数字不能替代切分策略。
- **本项目映射**：`retrieval.split_chunks` 兼容调用独立 `chunking.py`；默认 fixed 提供字符上限、局部边界、重叠和精确偏移；structure 已在纯计算预览中提供章节与句段边界。
- **差距**：入库仍使用只识别换行和中文句号的 fixed，结构策略尚未进入持久化检索；两者都没有语义模型。Azure 示例参数与 Token 上限不能证明 900/120 最优。

### S02 · LangChain：先按 Markdown 标题分节，再在节内控制大小与重叠

来源：[Split markdown](https://docs.langchain.com/oss/python/integrations/splitters/markdown_header_metadata_splitter)。类型：维护方集成文档，重点为页末 `chunk_overlap` 说明。

- **来源事实**：标题切分保留各节元数据；后续大小切分在各节内应用重叠，重叠不会跨越章节或文档边界。
- **技术启示**：章节划分和长度限制应分层处理；跨主题重复上下文并非必要。
- **本项目映射**：structure 预览已维护 `heading_path`、章节区间和节内重叠；默认 fixed 和现有入库片段仍无这些结构元数据。
- **差距**：这是本地标准库实现，未调用 LangChain；不能把 fixed 的“寻找最后一个换行”描述为标题切分，也不能把预览能力当成现有知识库已完成结构重索引。

### S03 · LangChain GitHub：标题栈、代码围栏与原文保留需要显式处理

来源：[langchain_text_splitters/markdown.py](https://github.com/langchain-ai/langchain/blob/master/libs/text-splitters/langchain_text_splitters/markdown.py)。类型：维护方源码；核查了 `MarkdownHeaderTextSplitter.split_text` 与 `ExperimentalMarkdownSyntaxTextSplitter`。

- **来源事实**：实现维护标题层级和代码块状态；同文件另有实验性实现强调保留原始空白及语法元数据。普通标题切分路径包含清理与重新拼接文本的操作。
- **技术启示**：不能仅用匹配 `#` 的正则处理所有行；代码围栏内的 `#` 不应产生新章节。结构元数据与原文切片应分开保存。
- **本项目映射**：结构预览按原始行扫描，维护 ATX 标题栈和反引号/波浪号围栏，围栏内不识别标题；标题元数据与精确原文切片分开。
- **差距**：借鉴结构识别思路，没有直接照搬清理或拼接文本的实现。未完整解析 Setext、表格、HTML 或 DOCX 样式，也不把实验性组件当成完整 Markdown 标准。

### S04 · LlamaIndex GitHub：段落、句子与字符兜底的分层切分

来源：[node_parser/text/sentence.py](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/text/sentence.py)。类型：维护方源码；重点为 `SentenceSplitter`、`_split_fns`、`_sub_sentence_split_fns`、`split_text_metadata_aware`。

- **来源事实**：实现优先利用段落和句子，必要时降到更细分隔与字符；其大小和重叠按 Token 计量，并考虑元数据占用。
- **技术启示**：长段落仍需确定性的兜底；给模型的总上下文要计算元数据，而非只计算片段正文。
- **本项目映射**：structure 预览在章节内优先空行段落，其次有限中英文句末，最后字符上限；调用模型的路径独立核算完整上下文预算。
- **差距**：没有调用 LlamaIndex 句子 tokenizer，也没有准确模型 tokenizer。中英文标点启发式只属于结构/句末边界优化，不能宣称等价；英文缩写和编号仍有边界限制。

### S05 · Amazon Bedrock：检索子块，提供较大的父块上下文

来源：[How content chunking works for knowledge bases](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-chunking.html)。类型：AWS 产品文档，重点为 Hierarchical chunking。

- **来源事实**：层次切分显式设置父块、子块与重叠；召回先针对子块，返回时可以换成父块，因此最终结果数可能小于请求的子块数量。
- **技术启示**：检索粒度与供模型阅读的上下文粒度可以不同；扩大上下文时应去重，并受预算约束。
- **本项目映射**：已有 `chunk → document_id` 来源关联，但 `read_evidence` 仍读取单个片段或 Wiki。
- **差距**：尚未实现父块表、父子召回替换或父块阅读工具。原文所属关系本身不等于实现了 parent-child retrieval；也不能承诺该策略必然提高当前 BM25 的效果。

### S06 · LlamaIndex GitHub：父子关系是需要维护的数据结构

来源：[node_parser/relational/hierarchical.py](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/node_parser/relational/hierarchical.py)。类型：维护方源码；重点为 `HierarchicalNodeParser`、`_add_parent_child_relationship`、`get_leaf_nodes`。

- **来源事实**：实现递归生成不同层级节点，并显式保存父节点和子节点关系。
- **技术启示**：父子切分需要关系、版本与层级选择，不能靠给同一个片段更换名称完成。
- **本项目映射**：SQLite 已有不可变文档版本与片段 ID，可承接将来的关系扩展。
- **差距**：当前没有这套层级关系。引入前应明确原文更新如何使相关父子索引失效，以及引用仍如何落回原文位置；本期不需要为此安装框架或向量数据库。

## 3. Agentic 检索与证据流程

### S07 · LangGraph：取证、评估相关性、改写后继续检索

来源：[Build a custom RAG agent with LangGraph](https://docs.langchain.com/oss/python/langgraph/agentic-rag)。类型：维护方教程；重点为 `grade_documents`、`rewrite_question` 和图组装。

- **来源事实**：示例让模型决定检索，检索后判断相关性，并在需要时改写问题、再次检索，再生成答案。
- **技术启示**：Agentic 行为应由明确的控制流和工具调用体现，不能仅凭使用某个框架名称判断。
- **本项目映射**：`AgentEngine` 在真实模型模式中接收多轮工具调用，工具结果送回模型；运行次数、工具数与预算有边界。
- **差距**：当前没有独立的相关性评分节点或明确的改写节点，模型可通过下一轮工具参数表达改写。代码未使用 LangGraph 编排；脚本模型测试只证明控制流，不证明真实模型能稳定补查。

### S08 · Azure AI Search：查询拆解、并行检索、汇合及来源记录

来源：[Agentic Retrieval Overview](https://learn.microsoft.com/en-us/azure/search/agentic-retrieval-overview)。类型：微软产品文档，重点为 Architecture and workflow。

- **来源事实**：服务可将复杂问题拆成聚焦的子查询，并行检索、排序与汇合；可以返回来源引用和执行活动信息。
- **技术启示**：复杂问题的覆盖率与可审查性需要查询计划、来源和活动记录共同支持。
- **本项目映射**：当前记录公开工具选择、参数、命中来源与最终引用，也允许模型连续搜索不同子问题。
- **差距**：同轮工具在当前代码中顺序执行；没有独立 decomposition 计划对象、并行检索、语义重排或 Azure 服务接入。不能把“允许多次搜索”写成已实现 Azure 式并行查询规划。

### S09 · Anthropic：给片段补充来源上下文，并用评测检验收益

来源：[Introducing Contextual Retrieval](https://www.anthropic.com/engineering/contextual-retrieval)。类型：厂商工程文章及实验报告。

- **来源事实**：文章介绍在嵌入与 BM25 建索引前，为片段加入解释性上下文，并比较多种检索配置；强调在自身场景中评测。
- **技术启示**：片段中的代词、时间和对象可能脱离全文后失去意义；补充来源信息有明确的设计动机。
- **本项目映射**：当前 BM25 重复加入文档标题，证据包含来源键与版本。
- **差距**：标题加权不是完整 Contextual Retrieval；尚无模型生成上下文、嵌入混合检索或 reranker。可先评测确定性的 `文档标题 + 标题路径` 元数据，但这是本地简化方案，不能照搬文章中的提升比例。

## 4. Wiki 来源、版本与失效

### S10 · Microsoft GraphRAG：派生知识需要回连 TextUnit 与 Document

来源：[GraphRAG outputs](https://microsoft.github.io/graphrag/index/outputs/)。类型：微软官方项目文档。

- **来源事实**：输出表区分 documents、text_units、entities、relationships、community_reports；文档与派生对象保留 text unit 关联，text unit 包含原文和 document ID。
- **技术启示**：原始资料与生成知识应分开，派生结论要保留可核对的来源关联。
- **本项目映射**：`documents → chunks → wiki_sources/claims` 保存来源链，Wiki 检索结果附带原文片段，最终回答重新检查来源有效性。
- **差距**：本项目没有实体关系抽取、社区发现、社区报告或 GraphRAG 查询算法。它是带引用与发布生命周期的 Wiki，不应命名为已经实现 Microsoft GraphRAG。

### S11 · LlamaIndex GitHub：稳定文档身份与哈希用于检测更新

来源：[indices/base.py](https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/indices/base.py)。类型：维护方源码；重点为 `refresh_ref_docs`。

- **来源事实**：实现按文档 ID 读取已有哈希；新文档执行插入，哈希变化时更新对应文档。
- **技术启示**：判断是否需要重建时应同时考虑稳定身份与内容变化，不能只比较文件是否存在。
- **本项目映射**：`source_key` 表示资料身份，正文哈希与标题共同参与幂等判断；更新创建新版本并保留旧记录。新增上传使用独立来源键，更新上传明确指定目标 ID/版本，避免按文件名碰撞隐式更新。
- **差距**：不可变历史、Wiki 发布和事务内失效是本项目额外设计，不是 `refresh_ref_docs` 提供的功能。当前没有跨版本 chunk 差分复用，也未实现外部目录自动同步。

### S12 · Amazon Bedrock：源文档变化后需要同步知识索引

来源：[Sync a data source](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-managed-sync.html)。类型：AWS 产品文档。

- **来源事实**：数据源新增、修改或删除后需要同步；同步处理相对上次的变化，使知识库反映新资料。
- **技术启示**：知识更新是系统流程，不能假设旧索引或旧摘要会自动保持正确。
- **本项目映射**：同一来源产生新版本时，原文更新与依赖旧来源的 Wiki 失效在同一 SQLite 写事务中发生；人工修订必须替换失效引用，保存新草稿后独立发布。
- **差距**：当前没有文件监听、定时同步或源删除接口。`stale → 人工核对 → 发布新修订` 是本项目根据派生知识风险选择的工作流；本来源没有规定必须采用这一 Wiki 状态机。

### S13 · Microsoft GraphRAG GitHub：增量机制必须核对实际比较规则

来源：[index/update/incremental_index.py](https://github.com/microsoft/graphrag/blob/main/packages/graphrag/graphrag/index/update/incremental_index.py)。类型：微软官方项目源码；核查函数 `get_delta_docs`。

- **来源事实**：核查时该函数根据既有与输入数据的标题集合识别新增、删除输入；这一个函数不能证明对同标题内容变化执行哈希比较。
- **技术启示**：写“支持增量更新”之前，必须明确到底以什么身份和变化信号判定增量。
- **本项目映射**：当前采用稳定来源键与正文哈希，规则与该函数不同。
- **差距**：不能把本项目哈希/版本/失效事务归功于 GraphRAG 已有实现，也不能据该文件推断整个 GraphRAG 系统的全部更新行为。本来源用于说明要核对实现，而非作为直接复制方案。

## 5. 当前约束下的实现与后续改进

结构策略已依据上述原则完成本地纯计算预览，默认导入保持 fixed。以下区分当前实现和后续建议，均不表示厂商规定的唯一方案或已验证的生产效果。

### 5.1 已实现结构边界预览，同时保留字符基线

`chunking.py` 新增独立 structure，保留 fixed 的 900/120 字符窗口作为对照。依据 S01–S04，先识别章节，再在节内按段落、句末与上限组织片段，用于测试当前 Markdown 政策长文的结构边界。`/api/chunk-preview` 可比较两个策略，不写数据库、不请求模型；持久化导入仍为 fixed。

已落实的实现规则：

1. **先记录原文区间。** 使用标准库按原始行扫描，识别 ATX 标题并维护标题路径；代码围栏内不识别标题。不删空白、不重写正文。元数据与 `text[start:end]` 分开保存。
2. **章节内分层选边界。** 先考虑段落；超长段再考虑中英文句末；无法找到可靠边界时保留字符上限兜底。英文缩写、小数与编号只能做明确、有限的启发式处理，不能宣称完整语言学分句。
3. **章节间不重叠，章节内保留有限重叠。** 这是 S02 展示的组织方式。本地围栏边界与尾部保护可能将重叠减为 0，返回实际 `overlap_chars`；structure 不保证处处等于 120。
4. **结构信息单独提供。** 返回 `heading_path`、章节 offset、策略和边界原因，不把补充标题前缀算进原文定位。当前这些字段只用于预览；将来供给模型时还须一起计入上下文预算。
5. **范围要明确。** 首版不宣称完整解析 Markdown 表格、HTML、DOCX 标题样式或程序语言 AST；超长代码块仍需有有界处理规则。原文解析与结构切分属于不同层。

`900/120` 保留以减少实验变量，仍是本地初值。性质测试已覆盖原文重建、偏移、重叠、标题/围栏边界，最新结果见验收报告；这些不证明检索收益。下一步应使用冻结语料与问题集比较召回、截断条款、跨章节混合、重复正文、上下文和时延，再独立核验答案。结构策略正式入库前还需策略版本和依赖迁移方案，不能只改默认值。

### 5.2 父子召回作为下一项独立能力

S05–S06 支持“小块检索、大块阅读”的设计。当前无新增依赖的条件下，可以在 SQLite 保存章节父区间，再为命中的子片段提供有界的章节阅读工具；这仍需父子 ID、去重、版本核验和预算测试。建议先完成结构切分与评测，再决定是否加入，避免把整篇文档回填造成的上下文增长误认为检索改进。

### 5.3 不因参考资料出现框架就新增依赖

本项目仍可用标准库、SQLite 和现有 HTTP 客户端实现结构扫描、版本关联及显式 Agent 循环。引用 LlamaIndex、LangGraph、GraphRAG 或云厂商文档是说明设计依据，不意味着已经安装、接入或完整复刻这些系统。

### 5.4 本轮检索缓存与中文规则的官方依据

新增核查详见 [RETRIEVAL](RETRIEVAL.md)，与 S01–S13 的切分/Agent/Wiki 来源分别记录：

| 一手来源 | 来源事实与本地映射 | 差距 |
|---|---|---|
| [Elastic CJK bigram](https://www.elastic.co/docs/reference/text-analysis/analysis-cjk-bigram-tokenfilter)、[Stop token filter](https://www.elastic.co/docs/reference/text-analysis/analysis-stop-tokenfilter) | 官方提供二元词项与完整 token 停词处理；本地保留单字/双字，移除完整问句结构，避免因复合词含一个功能字就误删“和田”等词 | 未使用官方 analyzer 或中文词典，不声称完整中文语言处理 |
| [Lucene BM25Similarity 源码](https://github.com/apache/lucene/blob/main/lucene/core/src/java/org/apache/lucene/search/similarities/BM25Similarity.java) | 核对正 IDF 与长度归一化；本地 BM25Index 缓存词频/长度/倒排和三种语料视图 | 本地 k1=1.5、标题重复计数不是 Lucene 默认或调优结论；未接入 Lucene |
| [SQLite data_version](https://www.sqlite.org/pragma.html#pragma_data_version) | data_version 只可跨时刻比较同一连接；本项目逐操作新建连接，所以不用它作为全局缓存代次 | 独立 retrieval_state.epoch 是本地设计，不是将 PRAGMA 更名 |
| [SQLite triggers](https://www.sqlite.org/lang_createtrigger.html)、[Isolation](https://www.sqlite.org/isolation.html) | 持久触发器在业务写事务推进 epoch，读取事务同时取得 epoch 与候选快照 | 每实例内存索引、变化后全量重建；没有分布式共享缓存或单文档增量倒排 |
| [SQLite FTS5](https://www.sqlite.org/fts5.html) | tokenizer 与外部内容同步规则需要明确核对；trigram 对短查询的限制影响二字中文需求 | 本轮没有切换为 FTS5，更没有宣称所有中文查询可由一个默认 tokenizer 解决 |

本地微基准比较的是旧式重复读库/分词、冷索引和暖索引的成本，三者共享相同修复后的锚点规则。它不能证明答案质量提高，也不能证明同样的耗时比例可外推到生产并发。epoch 缓存和查询最终直接读库复核共同工作，不把“缓存命中”视为来源永远有效。

### 5.5 评测分离与人工核验依据

[Microsoft Foundry RAG evaluators](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rag-evaluators) 区分文档检索与最终回答的评价，并分别考察依据与完整性。项目据此把有分级 qrels 的 Recall/MRR/nDCG 与人工 correctness/groundedness/completeness 分开；本地 0–4 评分表、匿名包和成对汇总是本项目协议，未安装或复现 Azure 评测 SDK，也未调用模型裁判。

已实现的独立评测详见 [EVALUATION](EVALUATION.md)：8 份第二领域虚构原文、dev/holdout 各 12 题，问题/答案与入库语料分离，报告保留文件哈希及设置。两组都是本项目可见自编题，不能写成第三方盲测；本轮只有纯检索和离线工作流，未评的答案三维保持 null。`--live` 是显式真实服务门禁，不是本轮已经运行的事实。

## 6. 实现现状与写作边界

本节已按当前代码与结构策略测试更新；默认入库和纯预览实验的范围仍须分别理解。

| 主题 | 已核查本地实现 | 当前不能声称 |
|---|---|---|
| 切分 | 默认 fixed 字符窗口；structure 预览的 ATX 标题/围栏/句段边界；精确 offset、参数校验与完整重建 | 900/120 已调优；语义切分；完整 Markdown 解析；结构策略已进入现有索引 |
| 检索 | 当前版本 BM25、标题加权、词法锚点过滤；批量读取与 epoch 内存缓存 | 稠密检索、EmbeddingProvider、向量索引、混合 RRF、reranker、单文档增量或生产吞吐已实现/验证 |
| Agent | 真实接口适配、有界多轮工具循环、公开轨迹、引用有效性检查 | 已验证真实模型收益；独立规划器；并行子查询；LangGraph 图编排 |
| Wiki | 引用原文的草稿、人工修订与 expected_version、独立发布、历史、原文更新事务内失效、备注排除检索 | GraphRAG 实体社区算法；自动保证语义正确；自动合并编辑冲突或无人工核对的可信自动发布 |
| Wiki 编译容量 | 文档数、片段数、上下文和 Token 预算分别设限 | 选择最多12份就保证所有长文都可一次编译；20片段是官方通用最优值 |
| 评测 | 原有来源回归；新独立语料检索指标、哈希、匿名人工评分和配对汇总协议 | 已验证真实模型优势、自动答案正确率、第三方盲测或生产 P95 |

特别说明：当前 Wiki 编译最多 20 个有效原文片段，是本地有界编译规则；它与 Anthropic 检索实验中比较的 top-K 不是同一指标，不能互相作为推荐依据。源引用存在和版本有效只能证明来源链完整，不能自动证明每条生成结论受来源语义支持。
