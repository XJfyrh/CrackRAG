# 离线缓存探针对审计

> 历史阶段记录：本页描述该增量完成时的范围；后续 M1 离线闭环进展见 [M1 使用说明](m1-offline-v1.md)与[验收记录](m1-offline-acceptance.md)。

**2026-10-07，合成证据检查，不是真实缓存测量。** 此工具把[测量草案](cache-measurement-v1.md)的同前缀 parent/fork 检查变为可运行的离线代码，复用[已有响应契约](provider-contract-v1.md)，不引入运行配置、传输、密钥、额度授权或真实结算。

## 运行与复核

从仓库根目录运行，Python 3.11+ 标准库即可：

```sh
python -B -m research.adc.cache_review
python -B -m unittest discover -s research/adc/tests -v
```

CLI 只向 stdout 打印 16 组手写合成案例，包括完整输入、期望值、审计结果与 `all_expected`。它不读外部文件、环境密钥或真实响应，不发网络请求，不写数据库。需要保存报告时可重定向到仓库外的新文件。`all_expected=true` 仅表示这些案例符合预设契约；`reported_positive` 的合成案例不是真实命中证据。

Python 入口为 `research.adc.cache_evidence.audit_pair(parent, fork, contract)`，其中 `contract` 直接使用现有 `RouteContract`。它只检查调用者传入的记录，不独立证实记录的真实性，也不驱动 P0 runner。`RequestSnapshot.freeze(request)` 将请求复制为不可变的 canonical JSON 字符串；JSON object key 必须是字符串，不支持非有限数或 Python 私有对象。

每条记录的字段：

| 字段 | 含义 |
| --- | --- |
| `attempt_id` | 非空调用标识；parent/fork 必须不同 |
| `parent_attempt_id` | parent 必须显式为 null；fork 必须精确指向 parent |
| `request` | 非空 model 与 messages；其他参数全部保留比较 |
| `response` | 已解码的非流式 JSON；response.id 必须存在，且两次不能相同 |
| `generation_metadata` | 可选 generation envelope；沿用已有 ID 关联检查 |
| `http_status` | 必须显式提供，不默认假定成功 |
| `dispatched_ns` / `completed_ns` | 同一单调时钟域的非负整数纳秒；合成案例是手写数字 |

不接受列表等非 object 的记录，非 JSON 输入抛出 `ValueError`；JSON 内的无效请求/响应尽可能返回明确审计问题。本模块不是面向不可信超大文件的网络网关。请求形状只做局部检查，不能据此断言工具调用、schema、上下文长度或模型参数受真实 API 支持。

## 三种哈希，两个一致性条件

- `request_sha256`：完整请求 canonical JSON（UTF-8、键排序、紧凑分隔符、禁止 NaN/Infinity）的 SHA-256
- `messages_sha256`：完整 messages 数组的相同序列化哈希
- `parameters_sha256`：除 messages 外所有字段的哈希，包括模型、工具/schema、路由、reasoning、输出上限和未知扩展参数

fork 必须在完全相同的 parent messages 后追加恰好一条非空 user 消息，且其余请求参数全部不变。报告分别给出 `shared_message_prefix_matches` 和 `request_parameters_match`。只有消息匹配才给出 `shared_message_prefix_sha256`；即使消息匹配，参数变化仍判为不一致。

这里是**消息结构与本地序列化一致性**，不是完整 JSON 字节串前缀关系，也不是供应商拼接、分词或 KV 前缀证明。message hash 不含 tools/schema，它们由 parameters hash 单独覆盖；不能只拿一个 hash 宣称完整输入相同。namespace 和正文若出现在既有消息中，其任何修改都会被发现；本轮不新增 namespace 编码或冻结配置系统。

此审计器只检查一对同前缀请求。不同 namespace、正文变体和延迟组需要分别形成各自合法 parent/fork 对，跨组比较、tokenizer 长度核对和统计并未实现。传入任意追加消息可以满足结构检查，审计器不判断它是否真正执行了 cracking，也不独立核实实际 open 事件或文档 provenance。

## 身份、时序与判定

- 不允许重复 attempt/generation ID、错误 parent 引用，或 parent 本身携带另一个 parent
- 两条记录都要求 completion 不早于 dispatch，fork dispatch 不早于 parent completion；只根据输入时间判断，不声称实际观察到了调度或持久化屏障
- 纳秒字段必须由调用者保证同一时钟域。相等时间允许，不能从非流式耗时推出 TTFT，也不把间隔视为缓存 TTL
- 预期 model/provider 名称直接使用 `RouteContract`。本审计版本要求请求 model 同样等于 `expected_reported_model`，尚不支持请求别名与返回 model 的显式映射
- `pair_status=consistent`：本地检查没有已识别的问题；`inconsistent`：至少一个不一致/非法字段；`incomplete`：仅存在未知/缺失证据
- 只有 consistent 才将 fork 的有效 cached tokens 标为 `reported_positive` 或 `reported_zero`；其他情况统一 `inconclusive`，同时保留已知 cached tokens 与每字段状态

parent 和 fork 都需结构可用、模型/provider 名称匹配、核心 usage/cost 已知且已识别字段无非法值、cached tokens 已知。可选 cache-write/reasoning 缺失或 null 本身不挡住此有限判定，仍原样记录；非法值会阻止判定。无论哪种结果，都不证明精确 endpoint/服务层、文档段完整 KV 命中或实验臂隔离。

## 费用规则

报告分别列两次 `usage.cost`，仅在身份关联可用、无重复且两者 cost 均已知时，用足够精度的 Decimal 相加，单位固定为 **completion-reported credits**。零有独立已知状态；缺失/null/非法 cost 不补零、不输出局部合计。

路由不符、HTTP 错误或截断输出也可能产生费用，因此它们不会擦除已知的 reported cost；不过此类对不能解释为可用缓存测量。generation 的 USD、价格表估算、缓存写入/折扣、reasoning tokens 不再加到该数值上，generation 原始记录保留在输入中而不用于填补 usage。此合计不是 settled USD、账户对账、全实验成本或节省率。

## 与 M1/M2 的边界

本轮增加 M1 的离线证据审计覆盖，未完成 corpus、通用代理循环、真实账户预留/恢复和 scorer adapter。P0 的账本依旧只接受 mock；本审计器没有 paid-readiness gate，不能开启 M2。真实测量仍必须完成[测量草案的前置条件](cache-measurement-v1.md#开始之前)，获得独立预算批准并验收 endpoint、账户与完整留档。
