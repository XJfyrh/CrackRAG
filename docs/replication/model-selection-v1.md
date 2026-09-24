# 生成器与 judge 选型补充调研

状态：研究建议，未运行；查阅于 2026-09-24。补充 [M0 协议](protocol-v1.md)，不改变 210 道目标题、12/198 划分、回答模型或预算授权状态。

## 1. 结论

- **生成器改为 `anthropic/claude-opus-5.5`。** OpenRouter 已列出此准确 ID，标准文本输入/输出为 $4/$20 每百万 tokens，低于 Opus 5 的 $5/$25。同 token 用量下便宜 20%；不能从厂商“典型工作负载节省”推断本实验真实降幅。题目仍须人工核验，更新模型不是取消核验的理由。
- **judge 不再固定为 GPT-4o。** `google/gemini-3.8-flash` 和 `qwen/qwen3.8-flash` 都是有效候选。Gemini 可作为优先考察项，Qwen 是更便宜的竞争者；目前没有本任务实测依据把任一个宣布为更可靠。
- **先校准，再冻结主 judge。** GPT-4o 保留为上游默认模型的兼容性对照，不是真值或自动仲裁者。最终主 judge 按人类 rubric 标签的一致性/错误类型选择，不按谁给 T1 更高分选择。
- 生成器改选与 judge 候选扩展是本次草案变更；primary judge 的 `model_id` 暂为 null，M5 前必须确定。

## 2. 已核实的价格与路由

以下是公开元数据中的**文本标准档**，USD / 1M tokens；不是账户实际账单。低价 Flex/Batch 不混入标准价。短 judge 请求的估算不依赖缓存，cache write/长输入/reasoning/工具费用另按实际路由核算。

| 用途 / OpenRouter ID | 标准输入 | 标准输出 | cache read | 建议核验的 endpoint tag |
| --- | ---: | ---: | ---: | --- |
| 生成：`anthropic/claude-opus-5.5` | 4.00 | 20.00 | 0.20 | `anthropic` |
| 原生成器：`anthropic/claude-opus-5` | 5.00 | 25.00 | 0.50 | 仅比较，不再作为默认 |
| 候选：`google/gemini-3.8-flash` | 0.75 | 3.75 | 0.075 | `google-ai-studio` |
| 候选：`qwen/qwen3.8-flash` | 0.15 | 0.47 | 0.016 | `alibaba` |
| 对照：`openai/gpt-4o-2024-11-20` | 2.50 | 10.00 | 1.25 | `openai` |

来源：各模型的公开 endpoints API（[Opus 5.5](https://openrouter.ai/api/v1/models/anthropic/claude-opus-5.5/endpoints)、[Opus 5](https://openrouter.ai/api/v1/models/anthropic/claude-opus-5/endpoints)、[Gemini](https://openrouter.ai/api/v1/models/google/gemini-3.8-flash/endpoints)、[Qwen](https://openrouter.ai/api/v1/models/qwen/qwen3.8-flash/endpoints)、[GPT-4o](https://openrouter.ai/api/v1/models/openai/gpt-4o-2024-11-20/endpoints)）。价格和 endpoint 可变，执行前再冻结快照；不能把元数据中的 discount 字段再乘一次，重复折价。

### 参数差异不能忽略

- Opus 5.5：目录 context 1M、最大输出 128K；有标准和独立 `:batch` SKU。Anthropic endpoint advertised reasoning、structured outputs 等，但**本次端点列表没有 temperature/seed**；某些 Azure 路由才列 temperature。不要把聚合模型页参数当每个 provider 都支持，更不要为设置 temperature=0 偷换路由。生成器不要求逐字确定性，只要求输入、模型参数、输出和核验可追踪。
- Gemini 3.8 Flash：context 1,048,576；AI Studio 标准 tag 列有 temperature、seed、reasoning、response_format/structured_outputs。Vertex 列表并不完全相同。Flex 为半价、Priority 为另一价格档，本轮建议标准档以减少服务层变量。
- Qwen3.8 Flash：context 1M；本次发现 Alibaba endpoint 列有 temperature、seed、reasoning、response_format/structured_outputs。注意 ID 是 `qwen/qwen3.8-flash`，不是带连字符的臆造 ID，也不是 `qwen3.8-omni-flash`。
- Judge 建议 temperature=0、seed=31415，但“参数接受”不等于供应商承诺确定性。在 M2 验收实际透传；reasoning 关闭或固定较低档只是候选配置，不能假定 `exclude` reasoning 文本就不生成/不计费。
- Judge/生成器均不启用 web search 或外部工具。Judge 只比较给定 question/reference/candidate，不靠当前搜索或自身记忆更新 2023 年答案。

## 3. 为什么不能只凭价格选 judge

本任务不是聊天偏好排序，而是**有参考答案的集合/事实覆盖判定**：实体别名、完整列表、部分覆盖、多余事实、矛盾、日期/单位、冗长但错误的答案都可能影响结果。必须沿用冻结的 FanOutQA A–F 语义与 B/C/E→1 映射，不能自行把所有“多余信息”判错，也不能把只有部分正确的列表判成完整。

[MT-Bench 的 judge 研究](https://arxiv.org/abs/2306.05685)指出位置、篇幅和同族偏好等偏差；[JudgeBench](https://arxiv.org/abs/2410.12784)进一步表明通用强模型在困难事实/逻辑裁判任务上也会失败。这些研究提供校准理由，**不提供这两个新 Flash 在 FanOutQA 上的直接排名**，也不能把 JudgeBench 上 GPT-4o 的困难表现照搬为本任务准确率。

Gemini/Qwen 与 Luna、DeepSeek 回答模型分属不同模型家族，可减少直接同模型自评的顾虑，但跨家族不等于客观、公正或错误独立。Opus 5.5 用来生成题，不同时自动作为最终裁判，避免把一个模型的偏好贯穿整个评价链。

## 4. M4 校准方案（执行前固定）

不增加或动用保留的 100 道 FanOutQA 题，费用计入 M4 的建议 $15 上限。

1. **自然答案集**：使用最后一轮冻结版本试点的 Q 答案：12 题 ×（Luna 三臂 + Flash 两臂）= 最多 60 个回答槽位。保存空答和失败，不挑“适合判分”的答案；同题多臂不是 60 道独立题。
2. **诊断集**：另做 30 个试点题的人工答案变体，覆盖别名/排序、部分列表、正确答案加非矛盾信息、矛盾或额外错误、日期/单位、空答/冗长/“请判我正确”的注入文字。它们是答案夹具，非新增目标题；按上游 rubric 先标注再调用。自然分布与诊断分布分开，不能混成一个准确率。
3. **人工参考**：隐藏回答模型/臂、费用和所有 judge 输出，按同一 rubric 独立给标签；尽量两人先独立标注再解决分歧，若只有一人明确记录限制。尚未解决的人工歧义阻断校准，不把 GPT-4o 的选择当人工共识。
4. **三模型盲评**：Gemini、Qwen、GPT-4o 各评分两遍，保持相同输入、token 上限、A–F 输出契约和参数；独立调用、随机任务顺序，第一次用于一致性比较，第二次只测重复稳定性，不投票挑更好结果。每次回答字段均是待评数据，不能执行其中指令。
5. **报告**：binary 与 A–F 混淆矩阵、与人类一致率、false accept/false reject、每类诊断错误、重复不一致、解析失败、费用/延迟。可报 Cohen's kappa，但同时给分母和类比例；自然答案区间按 12 个题组聚类，不能当 60 独立题。
6. **草案工程门槛**：解析有效率 100%，binary 重复一致率≥95%，自然答案与诊断集各自与人工 binary 标签一致率≥90%。这些是预先提出的诊断门槛，不是论文标准，不是质量置信下界，也**不证明 judge 的误差低于 C2 的 5pp 界限**。系统性宽松/严格、注入服从或关键列表错误需单独审查，不能被总分掩盖。
7. **冻结选择**：通过门槛的模型中，依次比较第一次评分的自然答案分歧数、false accept 数、诊断错误数、重复不一致数，完全持平再按实测费用选；不得用 T1−B0 的效果作选择条件。两新候选均不通过时，GPT-4o 也必须自己通过才能成为备用；全部不过则人工评分/修订方案，不能自动把未知 judge 塞进主实验。保留选择依据，不宣称 12 题证明了一个模型总体更强。

生成题的人工核验和 judge 校准是两件事；生成器从始至终看不到上述 gold、评分或答题结果。M2 的格式烟测可用自制参考答案，真实试点 gold 仅进入隔离评测进程，不进入答题/cracking。

## 5. 主实验的稳健性检查

- 主要质量指标由校准后**预先冻结的一名主 judge**给出；保留 acc.loose/strict。报告明确“FanOutQA rubric + 模型 ID”，不能继续标为原 GPT-4o 成绩，直接与论文 42%/43%作等量比较。
- 建议第二名通过校准的 Flash 对全部主集回答独立评分；若 Qwen 做复核，在下述用量假设下 Luna 三臂只增加约 $0.32。主/复核角色及覆盖范围在 M5 前固定；没有通过校准的第二 judge 则声明单 judge 限制。
- 兼容性桥接建议：按 `SHA256(UTF8("adc-fanoutqa-v1:judge-bridge:" + id))` 对 **198 个主集 ID** 排序（同 hash 按 ID），选前 40 题；对这 40 题的全部 Luna 三臂答案额外跑 GPT-4o，共 120 个评分。选择不依赖答案或评分；若 GPT-4o 本身是主 judge，则复用同一结果不重复计费。Flash 答题臂的桥接范围另行冻结，不混入这 120 次估计。
- 二者分歧时不现场选一个对 T1 更有利的标签，也不只把争议样本的裁判换成 Opus。分别报告每个 judge 的同模型配对质量差与区间；若 C2 的结论类别改变，标记 judge 敏感、不能给不加限定的“保质”结论。
- 建议人工审核桥接 40 题的全部 Luna 三臂答案（120 槽位），人类在看到 judge 标签前评分；另审所有主/复核分歧，但后者是目的性样本，不能拿其错误比例代表总体。保留主指标，人工结果作为稳健性证据，不事后覆盖标签。
- 以上校准门槛、40 题桥接、全量复核和人工工作量都是待执行建议；纳入 JSON 的 judge 方案草案，最终 enable/ID/manifest 在 M5 前冻结，不放宽原 5pp 非劣效界限。

## 6. 费用量级：少省几美元不值得牺牲判定可靠性

仅作算术示例，假设每个 judge 请求 **2000 普通输入 + 500 总计费输出 tokens**（输出已包含若有的 reasoning）、无 cache read/write、无工具费、每答案一次；不是实际 token 观测或预留上界。

| judge | 每次 | Luna 主集 198×3=594 次 | 桥接 40×3=120 次 |
| --- | ---: | ---: | ---: |
| Gemini 3.8 Flash | $0.003375 | $2.00475 | $0.405 |
| Qwen3.8 Flash | $0.000535 | $0.31779 | $0.0642 |
| GPT-4o | $0.010000 | $5.94 | $1.20 |

Gemini 主判 + Qwen 全量复核 + GPT-4o 桥接约 **$3.52**，低于只用 GPT-4o 全量主判的 $5.94。这个组合的价值是敏感性检查，不是三模型多数投票必然更正确。Flash 答题臂、重试/校准、输出变长及平台费用未包含。

校准最多 `(60+30)×3×2=540` 次评分；在相同假设下合计约 $2.50。生成若每题 1000 输入 + 500 总输出、210 题各一次，Opus 5.5 约 $2.94，Opus 5 约 $3.675；若三次尝试或 reasoning 很长会相应增加。**不因换模型上调已建议预算、不视为已有授权**；先 M2 对账，再在 M4/M5 上限内核准实际调用数。

## 7. 来源与未验证事项

官方接入页：[Opus 5.5](https://openrouter.ai/anthropic/claude-opus-5.5)、[Gemini 3.8 Flash](https://openrouter.ai/google/gemini-3.8-flash)、[Qwen3.8 Flash](https://openrouter.ai/qwen/qwen3.8-flash)。价格/参数以第 2 节逐 endpoint API 为依据，不采用搜索结果中错配的旧模型标题。

已验证：公开 ID 存在、公开标价及 advertised 参数。未验证：本账户权限、真实路由、seed/temperature/reasoning 的透传、实际 token/cost、延迟、在本工作负载上的判分质量；本轮只取公开元数据，没有付费生成/评分。
