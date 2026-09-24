# M0：FanOutQA cracking 实验协议 v1 草案

**PLANNED / 未冻结 / 未执行。** 2026-09-24；按最新决定缩为 **210 道原始目标题（12 试点 + 198 主集）**。这是[实验记录模板](../experiments/template.md)的研究版实例。工程设计见[开发计划](v0.2-plan.md)，固定参数见 [`protocol.json`](../../eval/replication/fanoutqa-v1/protocol.json)。本文与 JSON 冲突时停止冻结，修订一致后再执行，不任选一份。

## 1. 目标与身份

| 字段 | 内容 |
| --- | --- |
| 实验 ID / 状态 | `adc-fanoutqa-v1` / PLANNED；本轮只有文档与参数，无真实调用 |
| 负责人 | 仓库维护者负责付费批准、人工核验和运行；具体 run owner 在执行记录补齐 |
| 分支 / 基线 | `XJfyrh/experiment-adc-replication` / `50d4b7dbb5e77aa6266d6684197c90333cb66539`；执行提交尚未冻结 |
| 假设与决策 | 推测性结构化复用能减少后续文档读取和费用，且质量损失在预声明界限内；结果决定领域验证及产品化范围 |
| 研究身份 | 对论文核心机制的概念复现 + 明示扩展，不是原实现逐位重跑 |
| 范围 | FanOutQA open-book、固定历史 Wikipedia 正文、配对 R→Q、模型成本/质量/证据/延迟 |
| 排除项 | 任意生产任务保证、私有语料结论、论文 Hitchcock 原题、完整 RAG/oracle 结果、现有产品改造 |

## 2. 已确认选择与未决项

已确认：Luna 经 OpenRouter 为主模型；Flash 为复核模型；自建文件系统式导航和整页读取；历史设计仅参考、预算可合理提高；总选题 210。所有模型角色拟经 OpenRouter，不能把官方直连的配置原样混入。

| 角色 | OpenRouter model ID | 使用范围 |
| --- | --- | --- |
| Luna | `openai/gpt-6-luna` | 试点 B0/T0/T1，主集 198 组三臂 |
| Flash | `deepseek/deepseek-v4.1-flash` | 试点 B0/T1；主集复核规模在 M4 后、M5 前固定 |
| 生成器 | `anthropic/claude-opus-5.5` | 根据新调研替换 Opus 5；仅生成相关题，仍人工核验 |
| judge 候选 | `google/gemini-3.8-flash`、`qwen/qwen3.8-flash` | 先按人工标签校准，M5 前冻结一名主 judge；目前未选定 |
| judge 兼容性对照 | `openai/gpt-4o-2024-11-20` | 保留上游默认模型作为桥接，不是真值或自动仲裁 |

调研、endpoint 价格、参数差异与校准方案见[模型选型补充](model-selection-v1.md)。新增候选不增加或授权预算；210/12/198 划分及 Luna/DeepSeek 回答模型不变。

公开[模型目录](https://openrouter.ai/api/v1/models)可查这些 ID，且 [Luna endpoints](https://openrouter.ai/api/v1/models/openai/gpt-6-luna/endpoints)不止一个。目录可见、参数 advertised 不代表账户可用或网关完整透传；M2 才验收真实调用。研究只用环境中的 `OPENROUTER_API_KEY`，不要求把 key 发到聊天或写进仓库。

Luna 推荐 OpenAI Standard endpoint，**实际 endpoint/tag 和参数待 M2 验收**。固定 provider 白名单及服务层、禁自动 fallback，不混用 Flex/Fast/Azure/其他部署；每次记录实际路由。Flash 路由也需固定，不能把 OpenRouter 第三方价格当官方 DeepSeek 峰谷价。若能力或字段缺失，暂停、修改协议版本，不静默换模型。

### 冻结关口

M0 现在交付草案，M2/M3/M4 需要各自阶段授权；**M5 前必须填齐**：

- clean code commit、依赖锁、prompt/schema、tokenizer/解析/分页版本、workload 哈希、corpus/search 获取策略及初始缓存 manifest 哈希（可为空缓存的摘要）；实际动态发现的页面/查询清单在运行后封存，不要求提前知道未来代理查询；
- 各角色实际 endpoint、API 形态、reasoning/temperature/seed 支持情况、cache/结构化输出配置；
- 上下文上限、最大步数/输出、超限策略、fork 份额、每角色并发；
- 价格快照、费用上界算法、请求数/金额上限、批准人/日期、续跑流程；
- Flash 主集 ID/规模、是否启用跨模型 X；若未启用，明确 0，不留“视结果追加”；
- 210 道相关题的人工核验及许可，审计抽样规则、报告所用统计代码；
- judge 人工校准记录、主 judge ID、复核/桥接的启用与规模、各自 prompt/端点/参数、评分成本和独立报告规则；
- 默认每臂每题 1 次，主实验不自动重试；如需重复实验，在首次主集调用前固定重复次数并以题为聚类单位分析。

JSON 中未决字段为 null，`execution_ready=false` 指 **M5 主实验尚未可执行**。M2/M3/M4 使用独立阶段 manifest：先批准本阶段金额/请求数/并发，固定本阶段路由、输入范围和安全准入配置，才可真实调用；不要求它们提前具备由自身产出的全部 M5 科学冻结材料。任何阶段无授权都不得调用；全套科学冻结门禁只用于 M5，不能循环阻止用于解除未决项的探测。

## 3. 来源、版本与选题

### 3.1 论文与数据

论文：[arXiv:2608.31082v2](https://arxiv.org/abs/2608.31082v2)，2026-09-04，标题 Agentic Context Cracking；下载 PDF 的 SHA-256 为 `b3e85837702d01b89f77ea86e0ebeee79776d63e424f72b18c4b07d2c9d78e63`。本文参考数字来自该版本，不从搜索摘要推导结论。

FanOutQA：[固定 commit](https://github.com/zhudotexe/fanoutqa/tree/989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33)，包声明版本 1.3.0；读取日期 2026-09-24。

| 文件（仓库内相对路径） | 题数 / 用途 | SHA-256 |
| --- | --- | --- |
| `fanoutqa/data/fanout-final-dev.json` | 310；从中选 210，有 gold 和 decomposition | `b62a9797732c716e6b17ba4086f277d154d747ce2fa01614cb76a3372e7fb88c` |
| `fanoutqa/data/fanout-final-test.json` | 724；本实验不用，缺公开 gold | `e823838ab00d4fe875e0f9921ce07a7abe8806e8b380ffee17438d4bf76a68c5` |

不是旧 `-nov23` 文件。Dev 的证据在 decomposition 节点，不在顶层 necessary_evidence。主实验不访问 gold 分解来做工具检索或初始化对象库。论文只写 full FanOutQA，未从已读材料确定其 split/版本；不能把此 dev 子集称为论文原样本。

### 3.2 210 题的确定性划分

对每个 dev ID 计算 `SHA256(UTF8("adc-fanoutqa-v1:" + id))`，按十六进制升序排序，极端 hash 相同时按 ID 升序。**前 210 个为选集**；其中前 12 个试点，其余 198 个主集；最后 100 个保留。本规则不按答案、类别、证据页数或预期收益筛选。

集合摘要使用：ID 字典序排列、以 LF 连接、末尾无换行、UTF-8 编码后 SHA-256。

| 集合 | 数量 | ID 集合 SHA-256 |
| --- | --- | --- |
| 全 dev | 310 | `8ff3f869bd674da915d9e414e5b6be83b4e470739b2a4159b2cd5e0140b0da13` |
| 选集 | 210 | `5528f9818de9429e9c02f39096b3918204eb32b724d37121753ab3fd1f7fdbfe` |
| 试点 | 12 | `df01e1e64a6d47e27eb7db0c9c96f8127e243d76a6ed08197cd620e4da094cb3` |
| 主集 | 198 | `d4ee29259a20bc6faf3a1cb244d643b171cc758da135ac5b0f55eab7f7bedc78` |
| 保留 | 100 | `ae69fdffe0f1c50200695e1efc1f57f0970487d2067cfb8a7ad113c2939f3256` |

12 个试点 ID 按选择顺序：

```text
a284cc925636d80b d69d3c78f0e57713 d648ac0aeada9c7f 28c4715bc3cf75aa
0a950dfa942cf3ad 5aa09932011c8fcd c6707339a044ae15 6eabcceb8e365b7c
e082a9732cc2a366 5e660d80b1608a8a c58ac65553f24640 542983d2cb1630e0
```

M1 用自制夹具；M2/M4 只用这 12 题。既往查看过的题面/样例须登记，任何实际用于调参的主集题不能继续声称独立；若发现污染，冻结前公开修订 manifest，不偷偷从保留集补题。无需使用那 724 道 hidden-test 题做开发。

此前对全 dev 的描述性统计：每题证据页均值 6.82、中位数 6、范围 2–46；217/310 题至少与另一题共享一页。这不等于这些题实际可复用相同关系，也不能用作 210 子集的实测统计。它提示跨组对象共享会干扰归因，因此主协议隔离每组 store。

### 3.3 语料可复现边界

按 FanOutQA [utils.py](https://github.com/zhudotexe/fanoutqa/blob/989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33/fanoutqa/utils.py) 和 [wiki.py](https://github.com/zhudotexe/fanoutqa/blob/989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33/fanoutqa/wiki.py)的 dated 路径，正文 epoch 为 `2023-11-20T00:00:00Z`。记录实际 revid、正文/解析哈希和空页/历史修订缺失；不能保证每个修订与数据 gold 完全一致。

搜索仍是当前索引，但向模型只返回标题、ID、链接，不返回当前事实摘要，避免绕过历史正文；当前标题/排名及不同 query 的时间漂移仍是限制。首见 query 结果记录/重放。M5 前冻结获取策略和初始缓存 manifest（可为空），主实验结束再封存动态发现的完整搜索/正文 manifest；不能用 gold 清单提前枚举未来工具路径来凑哈希。Kiwix 不作本轮主路径：2023-09 ZIM 与该 epoch 不同、适配器 revid=0；若改用 Kiwix，必须新版本、固定 ZIM hash 和新的文档身份规则。

## 4. 工作负载生成与信息隔离

每个选中的目标题 Q_i 生成 1 道相关题 R_i；生成器只见 **Q_i 题面 + 以下规则 + corpus 日期**，看不到 gold、decomposition、证据清单、cracking 设计或运行结果：

1. 可从指定 Wikipedia 历史正文回答；与 Q 有重叠实体，目标属性不同。
2. 不重复、不改写 Q，不请求相同属性；不把 Q 的答案直接塞进 R 的题面。
3. 保持可理解的多实体/多文档问题，不为了对象库容易命中而人为指定关系或证据页。

人类核验在对应 R→Q 成对回答试验之前完成（M2 可先用自制夹具/试点 Q 做工程烟测，但不能将未核验的 R 用作有效预热）：记录 Wikipedia 支持页面/修订、实体重叠、两题属性、通过/拒绝及原因。核验者不看 T1 输出，不依据“模型是否能抽到”筛题。优先 accept/reject，不直接编辑注入 gold；每题最多 3 次生成尝试，全部计费并保存。3 次失败则进入人工处理/协议待决，**不可悄悄删题、换题或把未核验 R 当合格预热**；尚未解决不能冻结完整主实验。210/12/198 是当前用户确认的规模；任何缩样、换题或改变划分都须用户重新明确批准，再显式修订并报告选择偏差，不能仅凭维护者更新 manifest 改变规模。

210 指原始 Q；对应 210 道 R，Luna 主集有 `198×3×2=1188` 次问答执行，再另加 forks、judge、试点等 API 请求。R 不计入主准确率，Q 才评分。每题重置会话和笔记，组内对象历史可保留，组间不得保留。

FanOutQA 数据许可为 [CC-BY-SA-4.0](https://github.com/zhudotexe/fanoutqa/blob/989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33/fanoutqa/data/LICENSE)；相关题拟置于独立数据许可目录并保留归属、修改说明。Wikipedia 引用按相应许可/归属处理，不能笼统归入仓库 MIT。原始数据与全文响应本轮不入库，公开前做许可和敏感信息审查。

## 5. 实验臂和相同条件

| 臂 | R→Q 的处理 | 目的 |
| --- | --- | --- |
| B0 | 两题均独立普通代理，无 catalogue/对象读取/后台 cracking | 对照 |
| T0 | 同一受限 store/catalogue/read/fork；只抽当前题需要的结构，不扩展到可能的未来属性；同样能见已发生文档查询历史 | 控制“只缓存当前需求”的增益 |
| T1 | 同 T0，但允许基于当前/此前问题推测文档中的相关结构 | 主要机制 |
| Flash B0/T1 | 在固定 Flash 路由上各自运行完整 R→Q | 同模型复核，不与 Luna 费用混池 |
| X，可选 | 复用封存的 Luna T1 的 R 后 snapshot，由 Flash 新会话回答 Q | 真正的跨模型 store 复用；不是“两个模型各跑一次” |

T0/T1 的查询历史、总 cracking 预算、对象校验和接口相同，只有抽取范围指令不同；不能同时去掉 T0 历史再把差异全归于 speculation。`requestedness` 是模型自报，需审计。

省略精确重复/近重复 QA-cache 基线：不同目标属性不构成安全的完整答案命中；这**不排除共享中间事实或实体列表复用**，由 T0 衡量，不能声称任意语义缓存必然零命中。

相同条件：基础 prompt/日期/答题格式、搜索与分页、上下文清理、工具预算、模型路由和 reasoning 设置、评分规则；区别只在声明的工具/目录/抽取指令。默认每题每臂一次；主集按哈希顺序，臂顺序用固定种子 `20260924` 平衡/打乱，并保存调度 manifest。不能先跑完 B0 再长期延后跑 T1。

每个 `(model,arm,group)` 对象库初始为空。R 的所有 forks 完成后封存 snapshot，Q 从该 snapshot 开始；当前题的写入只对下题可见。Q 仍正常触发 cracking，其成本不能因本实验没有后继题而删去。

语料字节缓存共享、持久对象隔离。为避免 T0/T1 跨臂互相预热而 B0 无此优势，所有臂在最早可控 prefix 放固定格式、无语义的 `(experiment,model,arm,group)` namespace，并控制其 token 长度开销；同臂同组 parent/fork 相同。额外 cache key 不能替代 prefix 隔离，臂顺序随机化也不能消除非对称预热。M2 用受控 probe 对比同/异 namespace 的计费命中量，保留结果；不能仅凭总 cached_tokens 声称某段 KV 精确命中。无法验证隔离时须在 M5 前修订为“带缓存干扰的成本条件”，C1 不再声称纯粹来自持久对象复用。供应商隐藏系统/工具前缀仍可能共享，分别记录限制。

X 的完整成本必须带上 Luna 的 R/构建费用；复用既有 snapshot 虽不新增该笔现金支出，也不能在 X 的工作负载核算中把它算成免费。跨模型不变的只是文档/对象表示，不承诺两种 tokenizer 相同。

RAG top-5/top-10、人工核验的 oracle store、累计跨组 store、Hitchcock 式长调查不属于主试验；如做，各自单独版本。oracle 可以利用分解辅助人工构建，但不能把问答对自动转换当成已正确的完整关系库，也不能让其 gold 对象流入 B0/T0/T1。

## 6. 指标、核算和主张判定

### 6.1 质量与对象

- `acc.loose`：FanOutQA `answer_in_text` 得分的均值；`acc.strict`：完全命中题目的比例。沿用上游规范化，记录空答/弃答/拒绝；它们不是正确弃答的专门基准，不能将空答作为正确拒答奖励。
- 主 judge：模型尚未选定，按[试点校准方案](model-selection-v1.md)在 M5 前冻结；拟 temperature=0、seed=31415（逐 endpoint 实测支持），固定 reasoning 和输出上限。保留上游 A–F 规则、B/C/E→1 和 4000 字符处理，不因换模型改变评分语义。gold 只进隔离评测；隐藏臂标签并打乱顺序。没调用/失败记缺失，不把默认 0 当已测分数。替代 judge 的分数须注明模型，不与论文绝对准确率直接等同。
- Judge 稳健性：拟由第二名通过校准的 Flash 全量独立复核，并按固定 salt 从主集选 40 题的全部 Luna 三臂作 GPT-4o 桥接/人工审查；主模型、复核启用、覆盖与 manifest 均在 M5 前冻结。不得事后只替换分歧题标签或对 T1 有利的评分；报告各 judge 的配对差，C2 结论类别改变则标记 judge 敏感。校准≥90% 一致率的诊断门槛不证明误差低于 5pp。
- 工程/机制：每题文档打开数、成功对象读取数、实际返回对象/列表、fallback、无打开即答、fork 状态、cached/write/miss/reasoning token、完成时间。对象返回不等于被最终答案真实使用；需要可追踪引用或人工审计。
- 审计：**按模型、cracking 臂分别建抽样框，不混池 T0/T1/X**。单位为去重的 singular 对象或完整 list group，不把展开后的列表边当独立样本。每框分“被返回/未被返回 × singular/list”四层，每层均匀无放回抽 50 个单位，不足则全查（seed=20260926，抽样实现/hash 在冻结时确定）。保存每层总体数 N、样本数 n、纳入概率 n/N、题/来源和抽样 manifest；按题聚类评估区间限制。分别报告 singular 语义错误率、列表整组语义错误/不完整率；若合并 returned/unreturned，只对同种单位按 N 加权，不直接混合对象与列表组分母。检查关系、值/单位/日期和证据，成员级统计必须保留列表聚类，不因展开列表虚增有效 n。GROUNDED 通过率不是对象正确率。

### 6.2 三项明细、两种成本视角

记 A_i 为回答路径所有请求费用，K_i 为本题 fork/构建费用。失败请求计费同样归入所属题。

1. **测试题视角**：分别报告 `A(Q)`、`K(Q)` 和 `A(Q)+K(Q)`。论文未提供足够细项来消除 headline 是否含 cracking 的歧义，不能替它选择更好看的解释；本协议成本主指标用含 K 的值。
2. **全序列视角**：`A(R)+K(R)+A(Q)+K(Q)`，B0 的 K=0。R 是真实工作负载的一部分，不假定免费预热。只有该视角也支持时才称整段工作负载降本。
3. 生成/judge/probe/重跑作实验开销单列，并计入预算总额。最终平台未对账费用保留为 reported cost；UNKNOWN 不能丢失。重用的 R snapshot 在跨模型臂做成本归属时需计回，但现金总账不能重复扣款。

每请求保留总输入 P、cache read H、cache write W、剩余普通输入 U；在确认 API 含义后应满足 `P=U+W+H`。输出 O 包含 reasoning 时不得再次加 reasoning。无字段不作 0、不由总价反推伪造 token。物理 prefill 工作量不可直接从计费输入推断：报告总输入和非缓存输入 `U+W` 两种代理指标及定义。

费用报告同时列：OpenRouter reported/对账金额、固定本次 endpoint 价格表重算、Haiku 4.5 价格表敏感性重算。按截至查阅时 Luna Standard 短上下文每百万 tokens 为 U=$0.10/W=$0.125/H=$0.01/O=$0.50；Haiku 4.5 的 5m 表恰为其十倍。**这只在相同 token 分类和价格档下使重定价成比例，不使实际模型行为、缓存或降幅相同。** >272K 的 Luna 价格档需单独计入，不能拿累计每题输入与单次阈值混淆。Flash 用冻结的 OpenRouter endpoint 表；官方 DeepSeek 峰谷重定价若做，只作敏感性分析。

### 6.3 主张与判定

区间均为配对题级 bootstrap 的 95% 区间，10000 次、seed=20260925；成本主统计量为 `sum(T1 cost)/sum(B0 cost)`（均值之比），不是平均逐题比。独立排序中位数之比与配对比率分位数分开。

| 主张 | 论文参考（不是验收门槛） | 本协议判定 |
| --- | --- | --- |
| C1 成本，主要 | FanOutQA $0.26→$0.12，约 −53% | 分别判定 C1-test（含本题 K）和 C1-sequence：比率 CI 上界<1 支持，下界≥1 不支持，其余无法判定；另报点估计距 0.47 的差异 |
| C2 质量，主要 | judge 43%→42%，p=0.39；string 76.1%→74.2% | 预设 judge 差 `T1−B0` 非劣效界限 −0.05：CI 下界>−0.05 支持，上界<−0.05 不支持，其余无法判定；loose/strict 同时报告，明显矛盾需解释 |
| C3 避免文档输入，次要 | prefill 189K→87K，decode 较小 | 报输入/非缓存输入/输出/打开数的配对差与区间；非缓存输入比 CI 上界<1 支持方向，下界≥1 不支持，其余无法判定；标注指标定义差异 |
| C4 构建开销，描述性 | 每题约 4K decode、+12%、约 150 对象 | 固定 4096 输出预算；报告 `sum K(R)/sum A_B0(R)`、测试 K、对象数和截断/耗尽率；明确本分母定义，不能机械要求等于 12% |
| C5 收益分布，次要 | 中位成本 $0.246→$0.072；配对 p10 约 1/9，p90=1.24；约 3/4 题受益 | 报相同分布；受益占比 `Pr(T1 cost < B0 cost)` 的 CI 下界>0.5 支持“多数受益”，上界<0.5 不支持，其余无法判定；不把多数门槛当成复现 75% |
| E1 推测性贡献，扩展 | 论文机制解释，非其独立消融结果 | T1/T0 成本比和质量差按相同方法报告；只归因于本冻结指令差异 |
| E2 跨模型，扩展 | 持久结构可跨模型使用 | X 与相同 Flash B0 配对，含源 store 成本；规模不足或未运行标无法判定 |
| Q 对象审计，扩展 | grounded 不等于已证明语义正确 | 分层报告语义错误、列表不完整和证据错误，无数据则无法判定，不以 JSON 合法率替代 |

C1/C2 为共同主要目标；只有成本和质量同时满足才用“在声明界限内保质降本”，还须注明 test 或 sequence 口径。成本便宜但大量失败不得包装为成功。C4 是描述性结果，不能用事后阈值套“支持”；次要/探索项不冒充经多重检验控制的确认性结论。另报 judge 二元结果的 exact McNemar 双侧检验及不一致对数；p>0.05 不是非劣效证据。

198 组的质量区间可能较宽；缩小样本不放宽 5pp 界限，不保证功效。零 B0 成本的逐题比不可除零，单列计数；均值比的分母若为零则无法判定。

### 6.4 缺失、失败与停止后报告

正常发起却空答、超限、拒绝等，主准确率按未正确回答保留分母，费用照计。judge 缺失单列并给正确率边界/敏感性分析，不捏造标签。全局停派导致尚未执行的组标未运行，不能作为已测 0 成本；计划样本量、完成数与成对缺失数并列，主结论标阶段性/无法判定。完整配对子集仅作注明选择偏差的补充分析。

## 7. 试点门槛和冻结纪律

最多 3 轮、仅 12 道试点，版本和所有成本均保留，不将最优轮当独立结果。可调 prompt、工具/上下文及推理强度，但不得查看主集回答或效果。门槛是工程诊断，不是要求 T1 必须便宜：

- 无未来题/gold 泄露；路由、对象和阶段账本隔离测试全部通过。
- 预留/结算可对账、UNKNOWN=0；usage 分类不重算、实际 endpoint 匹配。
- 真实 probe 能观察复用 prefix 的 cache read；报告 fork 命中率分布。无命中须定位网关/参数/调度原因或显式改为不依赖缓存的偏差实验，不悄悄 HOT_ONLY 筛选。
- 至少 95% 问答执行无 harness 故障；无半列表发布。T1 在真实试点能调用 catalogue/read/fallback；若无复用，诊断检索/指令，但“无复用”本身可成为负面研究结果，不能筛掉这些题。
- B0 `acc.loose<0.40` 或 judge 正确数为 0/12 时触发基线审查；这是宽松故障信号，不是匹配论文准确率的门槛。无法改进时也可记录低能力条件继续，但须冻结前说明限制，不按收益挑模型。
- 用实际调用数量和保守费用区间估算 198 组 + 选定附加臂，批准金额/请求数上限；成本高时先申请合理提高预算，或由用户重新明确批准调整规模/范围，再重新冻结；不能运行中偷减样本。

M5 开始后 prompt、语料策略、模型路由、预算分配、样本数和统计标准不变。需修改则停为 STOPPED、保留旧数据，新版本单列，不能合并为一次独立确认试验。100 道保留题不用于自动补题或追到显著。

## 8. 运行与费用

| 字段 | 规定 |
| --- | --- |
| 隔离环境 | 独立 worktree/私有 run directory、SQLite 与研究 key；不用产品 DB/Redis；只共享不可变 corpus 缓存 |
| 真实调用负责人 | 维护者；汇总全部模型角色和阶段费用，报告历史支出，不用新账本重置累计开销 |
| 建议金额 | M2 $3、M3 $10、M4 $15、M5 $100，合计 $128；**全部待批准**，是护栏建议而非成本实测 |
| 非金额上限 | 请求数、输出、并发和期限在阶段批准时填写；4096 是每题 cracking 输出预算，非整个代理输出预算 |
| 预留与 UNKNOWN | 按最贵允许价格档及最大输出预留，考虑 tokenizer 误差/缓存写入；费用未知保留占用、停止所有角色派发 |
| 停止与恢复 | 无批准/达任一上限/泄露/路由漂移/usage 不完整/列表原子性失败立即停派；故障和账本核对后维护者显式恢复同一 run/attempt |

禁自动重试。确定无计费且需要重试也需显式记录新 attempt；不能覆盖旧失败。已完成请求不重复调用，只有 generation ID 不足以保证网络层幂等。异步 forks 已在途时收集结果/结算，不假定取消即免费。

M5 预算包括选定 Flash/X 和 judge，不是每个模型各 $100；选题缩小后的真实费用待 M2/M4 估计，不能直接宣称按 210/310 比例下降。OpenRouter key 限额、充值手续费、汇兑记录均为外部约束/开销，单列且不取代本地账本。

## 9. 结果与结论交付

当前结果：**未运行；C1–C5、E1/E2、Q 均无实测结论。**

M6 目标 `docs/replication/results-v1.md` 应包含：冻结提交和运行命令、批准与实际费用、最终 manifest 哈希、逐题匿名化指标、全部失败/缺失、双口径成本与质量区间、分布图、对象审计和案例、偏差及不能外推的结论。原始问答/完整 Wikipedia 正文不自动公开；报告输出只引用审查后的可分发材料。

## 10. 参考与偏差摘要

- [论文 v2](https://arxiv.org/abs/2608.31082v2)：机制与报告数字；未获得完整实现/原 prompt，不把“未找到”写成作者必未发布。
- [FanOutQA open-book 参考](https://github.com/zhudotexe/fanoutqa/blob/989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33/reference/paper_benchmarks/run_openbook.py)：Kani 的 search 合并标题取页与 BM25 片段排序。本实验拆 search/open、整页或固定分页、保留答案格式/日期语义，**不是照搬原工具和提示词**。
- [FanOutQA judge](https://github.com/zhudotexe/fanoutqa/blob/989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33/fanoutqa/eval/llm.py)：默认模型及配置可覆盖；走 OpenRouter 是供应链/路由偏差，规则相同仍需验收。
- [OpenRouter 缓存](https://openrouter.ai/docs/guides/best-practices/prompt-caching)、[usage](https://openrouter.ai/docs/use-cases/usage-accounting)：网关字段/路由需实测，不把 sticky routing 作为缓存保证。
- [Luna 模型与价格档](https://developers.openai.com/api/docs/models/gpt-6-luna)、[Haiku 价格](https://platform.claude.com/docs/en/about-claude/pricing)：只用于记录价格结构/重定价，不代替账户实际账单。
- 总体偏差：新模型/供应商、198 道独立主集、历史正文+当前搜索、自建工具/分页/schema、组内 snapshot 可见性与异步调度、4096 包含 reasoning、明确非劣效界限。各项保持可追踪，不能因为预算或历史设计方便而省略声明。
