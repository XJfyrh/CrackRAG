# 最小缓存测量方案

**状态：设计草案，未执行；2026-10-07。** 本文接续 P0，规定未来 M2 要观察什么。没有发送模型请求，也没有修改阶段授权。先读[研究入口](../../research/adc/README.md)，接口字段见 [provider 契约](provider-contract-v1.md)。

## 要回答的问题

1. 同一正常回答请求完成后，追加 cracking 后缀的请求，是否报告了可计费缓存读取？
2. 更换实验 namespace、修改正文、延迟派发后，观测如何变化？
3. 模型、供应商、费用和缺失数据是否能形成可核对的记录？

本实验不测 FanOutQA 正确率，不证明整份文档的 KV 全命中，也不证明 cracking 的全序列收益。供应商总 cached tokens 可能包含共享工具或系统前缀。

## 开始之前

以下全部落实后才能执行；当前文档不是运行配置。

- 单独批准 M2 的金额、请求数、并发、时间范围与停止条件
- 冻结实际模型 ID、完整 provider endpoint 标识和服务层；完成账户可用性验收
- 确认该 endpoint 的缓存最小前缀、支持参数和 tokenizer 口径；不能用字符数冒充 token 数
- 冻结提示词、schema、输出上限、重复次数、顺序与延迟组；输出预算覆盖计费 reasoning
- 准备统一真实账本，原子预留、幂等结算与 UNKNOWN 停派已经验收
- 已批准的私有证据目录可持久化；密钥从安全配置读取，不进请求归档或公开产物

现有 P0 账本只接受 mock，它不能因为增加了离线响应解析器就承担真实账户的准入职责。

## 成对探针

只用自制文档与试点，不用主实验题、未来 Q 或 gold。每组使用新的固定长度 namespace；组内保持工具、模型、endpoint、schema 和输出配置相同。

| 探针 | 输入与顺序 | 比较目的 |
| --- | --- | --- |
| 相同前缀 | 正常 parent 完成并保存响应后，追加 cracking 后缀派发 fork | 检查本地可控前缀是否一致，以及供应商是否报告缓存读取 |
| 不同 namespace | 新组使用不同但同格式的 namespace，再做相同 parent/fork 顺序 | 检查组间污染迹象；不能宣称供应商共享前缀绝对隔离 |
| 正文变化 | 新组中只改变冻结正文的一个明确位置，保留变更前后哈希 | 观察前缀边界变化，不把任意一次 miss 当成缓存失效结论 |
| 延迟 | 新组中在 parent 完成后按冻结延迟再派发 fork | 测量选定延迟下的行为，不把观测窗口写成供应商 TTL 承诺 |

最低先跑前两类；正文与延迟组是否纳入、重复次数和顺序在付费前冻结，不在看到结果后补选有利组。试点可以改版，但旧版本、失败与费用全部保留。不同组的供应商全局缓存状态不能由本地数据库控制。

本地应保存两种哈希：完整请求哈希，以及明确定义范围的共享前缀哈希。`max_tokens`、工具/schema、reasoning、路由参数等单独留档；不能删掉变化字段后仍宣称“请求完全相同”。

## 每次请求保存什么

| 范围 | 必要记录 |
| --- | --- |
| 身份 | experiment/group、parent/fork attempt、generation ID、请求模型与返回模型 |
| 输入 | namespace、文档版本与哈希、原始请求/前缀哈希、prompt/schema 版本 |
| 路由 | 请求限定、返回的结构化路由证据、provider_name、服务层；缺失保留 unknown |
| 调度 | parent 完成、fork 计划时间、实际派发与响应完成时间、单调时钟耗时 |
| usage | prompt/completion/total、缓存读写、reasoning；区分缺失、null 与 0 |
| 费用 | completion reported credits、generation reported USD、独立估算、最终对账；保留各自来源和单位 |
| 结果 | HTTP 状态、顶层/choice 错误、finish_reason、解析问题与原始响应摘要 |

当前离线契约只处理非流式响应，不能从一次非流式总耗时推导 TTFT。若未来增加流式测量，要先验证 SSE 分帧、最终 usage、EOF 中断和计时起点，再另报首 token 延迟。

## 判定与停止

- **缓存观测支持**：身份与路由证据满足冻结要求，供应商明确返回有效 cached tokens。只能按实际字段说明命中量
- **未观测到命中**：明确返回 0；仍需核对长度阈值、路由与顺序，不能立刻归因于 cracking
- **无法判定**：字段缺失/null、费用未结算、路由信息不足，或响应不完整；不能补 0
- 模型/已确认 provider 与冻结配置冲突、存在不允许的 fallback、身份关联错误、预算超限或未知费用时停止新派发
- 不因错误自动重试。generation 对账查询也必须记录；得到新证据不自动覆盖原始 usage，冲突交由维护者判断

并列报告 parent、fork 和两次请求的合计费用。reasoning 若已包含在 completion tokens 中，不再加一次。缓存写入和上游推理费用不与最终账户成本重复相加；`cache_discount` 可能为负，不能把负值一概当解析错误。

## 后续交付

M2 应交付 endpoint 支持矩阵、完整失败清单、可核对费用与计费缓存观测，并明确“支持 / 不支持 / 无法判定”。真实报告落地前，本文件保持方案身份；M4/M5 的科学参数与授权仍由[既有协议](protocol-v1.md)管理。

## 原始来源

以下为 2026-10-07 查阅的官方文档，不是账户实测：

- [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
- [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)
- [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Router metadata](https://openrouter.ai/docs/guides/features/router-metadata)
- [Generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)
