# README Visual Generation Notes

[简体中文](README_VISUALS.md) | [English](README_VISUALS.en.md)

Generated on 2026-10-16 using built-in ImageGen. These are conceptual workflow and layered-module diagrams; runtime interface images remain actual browser screenshots.

## Final assets

- [Knowledge lifecycle](images/knowledge-lifecycle.png): evidence gathering, knowledge consolidation, and updates, retaining seven operational steps.
- [System architecture](images/system-architecture.png): browser and CLI share an execution core; the model interface represents a shared capability, while versioning and retrieval belong to storage.

The architecture image shows module layers. See [architecture design](ARCHITECTURE.md) for complete call responsibilities. Connections in the initial architecture draft were corrected in a second edit; only final images are public assets.

## Complete prompts

### Knowledge lifecycle

```text
Use case: infographic-diagram.
Create a finished, premium Chinese product workflow illustration for the GitHub README of 知证 · ZhiWiki, a real local knowledge workspace. This is a conceptual explanatory graphic, never a fabricated app screenshot.
Landscape 16:9, sharp high-resolution raster. Art direction: architectural knowledge archive, graphite black #151515, ivory #f0eee7, muted copper #e9bb8d, restrained amber for outdated knowledge. Expensive editorial typography, strong visual hierarchy, large readable Chinese labels, clean precise diagram logic, spacious composition. Subtle dimensional paper, frosted glass document sheets, copper connectors, matte archive blocks; tactile restrained 3D illustration combined with an exceptionally legible infographic. Use meaningful document, quotation, archive, version-change motifs; visual interest comes from distinct objects, not rows of identical boxes. No purple flowchart, neon, dashboard mockup, glossy generic AI brain, robot, excess glow, random code, meaningless arrows or tiny text.
Header upper left exact: "从资料，到持续更新的知识"
Small brand upper right exact: "知证 · ZhiWiki"
Main graphic has THREE generous left-to-right open zones, each with its own distinct visual object, linked by one clear directional copper path.
Zone 1 headline exact: "取证" ; show a source document and a magnified source excerpt with readable version marker "v1". Under this object, two clearly ordered large labels exact: "01 导入原文" then "02 提问取证".
Zone 2 headline exact: "沉淀" ; show quoted source lines connecting to an organized Wiki folio and a small copper publication seal. Two clearly ordered large labels exact: "03 整理草稿" then "04 审核发布".
Zone 3 headline exact: "更新" ; show original document changing from "v1" to "v2", a dependent folio turning amber with small status exact "待更新", followed by an ivory repaired/published folio. Three clearly ordered large labels exact: "05 原文更新" then "06 知识待更新" then "07 修订再发布".
Within zones a small unambiguous progression visually connects the ordered substeps; across zones read strictly left-to-right. Show source-to-knowledge dependency as a subtle copper link. Do not imply automatic publication: step 04 and step 07 are human review/republication.
Footer short exact: "结论有出处 · 修订有版本 · 更新可追踪"
Keep only these specified words and version markers. All Chinese must be correct, crisp and comfortably readable when displayed at GitHub content width. Leave at least 5% safe margins. Full opaque dark background. No watermark.
```

### Architecture initial specification

```text
Use case: infographic-diagram.
Generate a finished premium technical architecture infographic for the GitHub README of 知证 · ZhiWiki. Wide landscape 16:10, high-resolution raster, opaque background. This must be a clear factual architecture diagram with beautifully art-directed dimensional illustration, not an app screenshot.
Visual system: graphite black #151515, warm ivory #f0eee7, muted copper #e9bb8d, dark stone, frosted glass and finely machined copper. Tactile low-relief architectural diagram, subtle perspective only for objects, perfectly flat-facing readable text. Scholarly architectural archive mood matching a premium evidence/knowledge product. Large crisp typography, plenty of negative space, clean hierarchy. No purple, neon, robots, AI brain, random circuit patterns, dense tiny text, dashboards, fake logos, benchmark numbers, unnecessary labels. Icons symbolic only: browser window, terminal prompt, orchestration rings, document folio, magnifier, database cylinders.
Header exact "知证的系统架构"
Small top-right brand exact "知证 · ZhiWiki"
Subheader exact "共享业务核心，贯通知识调查与版本维护"
Diagram content and accuracy are paramount. Arrange a legible layered composition with straight orthogonal copper connectors and distinct arrowheads. ALL technical names exact. Differentiate execution from storage with different restrained materials, while avoiding repetitive small rectangle chains. The following is the complete graph; do not invent additional components, automatic publication, vector databases, queues, cloud deployment or authentication.
Top entry band: two entry groups, "浏览器工作台" at left and "CLI" at right. The browser group contains a small browser illustration above a "FastAPI" plaque with subtitle "JSON API · SSE". Clear arrow browser -> FastAPI. CLI is a distinct terminal illustration, NOT inside or passing through FastAPI.
Middle core band: TWO large central execution modules side by side, label "AgentEngine" with subtitle "调查 · 工具调度 · 预算", and "WikiCompiler" with subtitle "草稿 · 修订 · 发布". Here the second module represents the Wiki workflow alongside compiler; footer Chinese prose will clarify human publish. From FastAPI connect to both modules. From CLI connect directly to both modules, bypassing FastAPI. Use routing with minimal crossings.
On the right outside the core, an external service tile exact "OpenAI 兼容接口". Both execution modules have subtle clear outward copper connectors to it. It is a configured model API, not a local model.
Below AgentEngine, a clear evidence-tool plaque exact "搜索原文 / 搜索 Wiki / 阅读证据". AgentEngine connects down to this plaque. The tool plaque connects to the central lower shared foundation module exact "KnowledgeStore", subtitle "原文版本 · Wiki 依赖 · 事务".
Wiki workflow also connects directly to KnowledgeStore.
Bottom foundation: KnowledgeStore connects to TWO large distinct objects on the same level: a frosted index object exact "BM25Index" subtitle "epoch 感知缓存", and a dark copper database cylinder exact "SQLite WAL" subtitle "原文 · 片段 · Wiki · 运行记录".
A small tasteful footer exact "单机工作区 · 模块化单体 · 显式审核发布".
Maintain the hierarchy and readable labels above all. Labels should be comfortable at GitHub 900px content width. Leave 5% safe margin all around. No watermark.
```

### Architecture final correction

```text
Edit this architecture infographic. Preserve the high-quality black/ivory/copper archive art direction, objects, title, typography quality and overall composition. Correct the diagram semantics by SIMPLIFYING the connectors. This is a module-layer illustration, not an exact per-call routing diagram.
Critical corrections:
1. REMOVE EVERY EXISTING copper connector and every existing arrow entirely, including the incorrect horizontal AgentEngine -> WikiCompiler arrow, the incorrect upward arrow into CLI, and the two duplicate arrows around the model API.
2. Change the large right execution-module title from "WikiCompiler" to exact "Wiki 工作流". Its small subtitle becomes exact "编译 · 修订 · 发布". The original class WikiCompiler only compiles drafts, so the workflow title is essential.
3. Add a small clear section caption under the header exact "模块分层示意". Arrange the diagram into conceptual layers with subtle horizontal separators, WITHOUT drawing any arrow between AgentEngine and Wiki 工作流.
4. Reintroduce ONLY these simple, unambiguous connections:
- Browser -> FastAPI, a straight rightward arrow at the top.
- From FastAPI AND CLI, downward branches go into ONE shared horizontal thin copper connector BUS above the execution modules. From that bus, two separate vertical arrows go DOWN to AgentEngine and Wiki 工作流. The bus is just a line, with no new component or label. CLI is an input to that line, never the destination of an upward arrow.
- AgentEngine -> "搜索原文 / 搜索 Wiki / 阅读证据" -> KnowledgeStore, two simple downward arrows.
- Wiki 工作流 -> KnowledgeStore, one downward elbow arrow.
- KnowledgeStore -> BM25Index AND KnowledgeStore -> SQLite WAL, two downward branches.
5. Keep the "OpenAI 兼容接口" service tile on the right, BUT NO ARROWS touching this tile. It is a separate capability annotation. Directly under it add a short exact label "Agent 与 Wiki 编译共用". This wording carries the relationship accurately without ambiguous routing.
6. All other original words stay correct, especially "KnowledgeStore", "BM25Index", "SQLite WAL", "JSON API · SSE", "原文版本 · Wiki 依赖 · 事务", "epoch 感知缓存", "原文 · 片段 · Wiki · 运行记录".
7. Footer exact "单机工作区 · 模块化单体 · 显式审核发布".
Prioritize flat-facing, perfectly readable text, accurate flow, and clear separation. No extra invented dependencies, no new labels, no extra arrows. Opaque landscape background, no watermark.
```
