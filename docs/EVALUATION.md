# 第二领域评测与人工评分协议

## 1. 本轮状态与评测目的

本轮明确跳过真实模型调用。新增入口默认离线；`--retrieval-only` 只测原文检索。没有调用模型裁判，也没有生成自动“答案正确率”。报告中的 correctness、groundedness、completeness 在人工评分前均为 `null`。

原有星舟售后 24 题及 `zhiwiki/evaluation.py` 保持不变。新增雾岚作业平台运维语料位于 `examples/benchmark`，由本项目人工编写，包含 8 份虚构原文、12 道开发题和 12 道预留题。它与原售后领域分开，但**不是第三方基准、独立标注团队盲测或真实生产数据**。维护者能看到预留题；预留集只是固定分组，不能声称不可见盲测。

## 2. 原文、题集与版本冻结

- `corpus/`：访问权限、队列额度、失败重试、产物、配置发布、事件响应、维护和审计导出。
- `questions/dev.json`：允许检查错误、调整词法检索与提示的开发题。
- `questions/holdout.json`：同次版本冻结后再使用的预留题；调参若查看过其结果，应披露并重新建立未用题集。
- `manifest.json`：唯一语料清单；参考答案、关键点、qrels 不进入数据库或模型上下文。

题目包含 `reference_answer`、`key_points`、文档级 `qrels`。相关性分值 2 表示直接必需依据，1 表示补充依据，未列出的当前 8 份语料文档按 0 处理。没有相关文档的问题使用空 qrels；不能将它们当作零分相关性样本混入均值。

每次报告记录原文、题集、manifest 的 SHA-256，并列出文件摘要。另保存 Python 版本、核心实现文件摘要和固定运行设置。配对执行共享一份设置、同一临时语料；报告不保存密钥、密钥环境变量名、本地库路径或 URL 中的用户名、密码、路径、查询参数。

使用 `--split all` 时保留整体 `summary`，同时按 `summary_by_split.dev` 与 `summary_by_split.holdout` 分别汇总，避免只看混合均值而掩盖开发集与预留集差异。

模型名称和预算固定不等于云端部署可完全复现。当前适配器没有覆盖 temperature 等采样参数，报告明确标为服务端默认；随机种子只用于匿名评分顺序。后续真实评测应另记录服务端模型修订与采样配置，不能将匿名种子称为模型生成种子。

## 3. 独立检索指标

先对每题执行一次 `store.search(kind='document', limit=20)`，将结果按首次出现顺序去重为原文列表，再取前 k 个文档。默认 `k=5`，可设 1–20。该过程独立于 baseline/agentic 的模型工具调用，不以最终答案引用倒推出检索质量。

- **Recall@k**：前 k 个唯一文档中相关文档数量，除以 qrels 中所有分值大于 0 的文档数量。
- **MRR@k**：首个相关文档排名的倒数；前 k 没有相关文档为 0。
- **nDCG@k**：每项增益为 `2^relevance - 1`，按 `log2(rank+1)` 折减，再除以理想排序增益。

候选阶段上限仍为 20 个片段，去重后可能不足 k 个文档；报告保留该上限，不把结果表述为无上限文档召回。无相关文档题的三个指标为 `null`，单独记录是否返回空结果。空结果命中只是词法行为，不证明语义拒答能力。检索异常记录错误类型，指标为 `null`，并单独计数，不能借排除失败美化平均分。

## 4. 生成对照与运行门禁

非纯检索模式下，每题运行 baseline 和 agentic 两次。第一题按 baseline→agentic，第二题按 agentic→baseline，后续交替；同题的两种策略相邻，减少长时间分组执行的服务状态差异。每个试验保留状态、失败、公开轨迹、延迟、模型调用量、Token 用量与实际引用。

默认即使传入 `provider=openai` 的配置，也强制使用离线执行；只有显式传入 `--live` 且配置为 `provider=openai` 才允许调用模型。`--live` 与 `--retrieval-only` 互斥。本轮不执行 `--live`，也没有任何评分 judge 请求。

真实 Token 用量只在提供完整、有效 usage 时填写 `actual_token_usage`。缺失 usage 则保持 `null`；执行器预算估算仍保留在独立 `usage` 字段中，不能当作实测成本。离线模型调用为 0。未配置真实价格，因此不输出货币费用。

每臂每题仅一次，当前报告不计算显著性、不声称模型优势、生产 P95 或真实业务收益。失败试验不丢弃；分析时同时查看失败数量和已完成回答评分。

## 5. 匿名人工评分

一次生成评测输出三份文件：完整 `.json` 报告、`.review.json` 匿名评分包、`.mapping.json` 私有策略映射。评分包按照固定随机种子洗牌，去掉 strategy、调用量、时延与工具轨迹；映射另存并在评分冻结前不交给评分人。原始回答风格仍可能暴露流程，因此只称匿名去标签，不保证严格双盲。

评分包提供问题、原始回答、参考答案、关键点和实际引用文本。人工逐维填整数 0–4，未评保留 `null`；可以写说明，不能修改 `review_id`、答案内容或 `answer_sha256`。

| 分值 | correctness：事实与条件正确 | groundedness：引用支持 | completeness：问题覆盖 |
|---|---|---|---|
| 0 | 结论错误或完全未回答 | 实质结论无来源支持或编造 | 关键要求基本未覆盖 |
| 1 | 多数关键结论有错 | 多数关键结论缺依据 | 只覆盖少量关键点 |
| 2 | 部分正确但存在重要错误/条件遗漏 | 一部分有据，仍有重要无据结论 | 覆盖约一半，遗漏重要要求 |
| 3 | 基本正确，仅次要问题 | 主要结论有据，仅次要引用问题 | 主要要求覆盖，存在次要遗漏 |
| 4 | 所有关键事实与适用条件正确 | 实质性结论均可由提供的证据支持 | 所有必要关键点和条件均覆盖 |

对于应说明不足的问题，正确承认资料不足、不编造内容可按参考关键点评价；不能因没有肯定答案而自动给零分。对于执行失败或空回答，评分人可按上述规则明确评分，或保留未评；汇总会显示已评数量，不能把未评失败悄悄算作成功。

评分脚本要求完整报告、评分包与映射的 `benchmark_run_id` 一致，并校验试验映射及答案 SHA-256。旧评分表、改动答案、重复行、未知 ID、越界值、布尔值和非整数评分会被拒绝。部分评分允许保留其他维度为 `null`。

三维均值只使用该维度已经评分的行。同题两臂在同一维度都有分数时，才进入 matched-pair 比较，输出两臂均值、agentic减baseline的平均差，以及较高/持平/较低数量。没有配对时均值和差为 `null`，不补零，不按不同题的样本均值伪装成配对比较。

## 6. 可运行命令

从项目根目录使用现有解释器，无需安装包：

```powershell
& 'C:/Users/11873/anaconda3/envs/langgraph/python.exe' scripts/run_evaluation.py --split all --retrieval-only --output reports/benchmark-retrieval.json
```

此命令产出 24 道检索记录，没有生成试验，评分包和映射的 `rows` 均为空。要验收匿名评分工作流，可运行离线成对摘录；这也不会调用真实模型：

```powershell
& 'C:/Users/11873/anaconda3/envs/labelimg/python.exe' scripts/run_evaluation.py --split dev --output reports/benchmark-offline.json
```

人工填写离线评分包后汇总：

```powershell
& 'C:/Users/11873/anaconda3/envs/labelimg/python.exe' scripts/score_evaluation.py --report reports/benchmark-offline.json --reviews reports/benchmark-offline.review.json --mapping reports/benchmark-offline.mapping.json --output reports/benchmark-human-scores.json
```

默认脚本只执行 dev。`all` 会同时读取预留题并使其结果可见；记录这次使用，后续不要宣称该预留集未被查看。正式模型联调仍按用户后续决定执行，本轮所有报告只来自纯检索或离线流程。

## 7. 评价维度依据

[Microsoft Foundry 的 RAG evaluators 文档](https://learn.microsoft.com/en-us/azure/foundry/concepts/evaluation-evaluators/rag-evaluators) 将文档检索评价与最终回答评价分开，并区分依据一致性和回答完整性。本协议借鉴这种分离：检索使用明确 qrels，回答由人工分别检查事实、依据与覆盖。

本地 0–4 评分表与 Recall/MRR/nDCG 实现是本项目协议，不声称复现 Azure SDK 的全部指标、默认阈值或模型裁判，也未安装 Azure 评测组件。验证代码位于 `tests/test_benchmarks.py`。
