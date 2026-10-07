# ADC 研究：M1 离线闭环

## 当前入口

M1 离线工程闭环已接通，待维护者验收：本地 dated corpus/确定性分页、统一工具代理循环、单账户金额预留与对账恢复、序列化 fake HTTP transport、MediaWiki envelope/真正 HTML converter 导入、R/Q 隔离、答案封存和独立 FanOutQA scorer adapter。真实 API/缓存/费用验收仍属于 M2；没有 M3 相关题生成核验或 M4 judge 校准结果。

完整运行、依赖、账户与封存边界见 [M1 使用说明](../../docs/replication/m1-offline-v1.md)，检查与限制见 [M1 验收记录](../../docs/replication/m1-offline-acceptance.md)。

```sh
python -B -m research.adc.m1 --run-dir /tmp/crackrag-m1
python -B -m unittest discover -s research/adc/tests -v
```

基础闭环无需第三方依赖或 key；真实字符串 normalizer 的完整独立锁在 `requirements-scoring.lock`，HTML importer 锁在 `requirements-corpus.lock`。未安装后者时4个 genuine-converter 测试明确跳过；安装后可运行完整覆盖。所有合成输出都显式标注，不能称为 FanOutQA 实验或论文复现结果。

原 P0、10 组 provider 合成契约与 16 组缓存证据审计继续保留：

```sh
python -B -m research.adc --run-dir /tmp/crackrag-adc-p0
python -B -m research.adc.provider_review
python -B -m research.adc.cache_review
```

以下章节只描述历史 P0 runner，不是新 M1 入口的能力清单。

## P0 机制验证

这是 `v0.2-plan.md` 的 P0 机制验证，使用 Python 标准库和 SQLite，无 SDK、网络请求或模型费用。论文 v1 称 ADC，v2 称 ACC；目录沿用 `research/adc`。这里的成功不能视为论文结果复现、真实 prompt cache 命中或成本收益。

## P0 运行与副作用

从仓库根目录运行，要求 Python 3.11+，无需安装依赖：

```powershell
python -B -m unittest discover -s research/adc/tests -v
python -B -m research.adc --run-dir "$env:TEMP\crackrag-adc-p0"
```

测试只在临时目录创建 SQLite，结束后清理。演示只在指定的仓库外目录创建 `adc-mock.sqlite3`、SQLite WAL/SHM 和 `summary.json`，stdout 同时输出报告。也可设置 `CRACKRAG_RESEARCH_DIR`。CLI 拒绝仓库内输出目录；不会读取密钥、访问真实语料、调用外部 API 或更新 git。依赖约束见 `requirements.lock`。公共 Python 检查入口已纳入此测试目录。

再次运行同一命令会读取已保存的调用结果与发布记录，不重复派发；报告不改变。改变同一实验的语料、题目或预算会被拒绝。保留数据库才能续跑。P0 支持一个活跃 runner，不支持两个进程同时驱动同一实验；独立连接仅用于事务可见性与准入测试。

## 两文档、两属性、三组轨迹

夹具是自制的虚构运动员表格，Aster 的 points/rebounds 为 10/4，Beryl 为 20/7。R 只询问 points；R 及其分支结束后，Q 才创建并询问 rebounds。`Question.view()` 只包含当前题 DTO，不携带未来题、gold 或完整 workload。每题重建会话，跨题只继承对象库和实际已发生的文档查询历史。

| 组 | R 行为 | Q 行为 | R mock 调用（回答/分支） | Q mock 调用（回答/分支） |
| --- | --- | --- | --- | --- |
| B0 | 打开两份原文，不 cracking | 打开两份原文 | 3/0 | 3/0 |
| T0 | 打开两份原文，仅保存当前 points | 先查目录，未找到 rebounds，打开两份原文 | 3/2 | 3/2 |
| T1 | 打开两份原文，保存有出处的 points 和 rebounds | 先查目录，读取两个 rebounds 对象，不打开原文 | 3/2 | 1/0 |

三组 R 回答都为 `{Aster: 10, Beryl: 20}`，Q 回答都为 `{Aster: 4, Beryl: 7}`。共 22 次 **mock** 调用。对象读取后仍有一次模拟最终回答调用，不能把 Q 的调用数记作零。

`summary.json` 包含每题快照、工具轨迹、调用状态、parent/fork 对应关系、prefix 哈希与模拟 usage。每组保留 `cost_views.target_Q` 和 `cost_views.sequence_R_Q` 两种口径；Q 自己的 cracking 计入 Q 口径，R 建库开销计入完整序列。金额 `0` 的依据明确是 `mock_no_paid_request`；模拟 token 使用字符数估算，`real_cache_hits=null`。未结算调用金额为 `null`，不会伪装成已知零费用。

## 最小机制与验证范围

- `schema.py` / `schema.sql` / `store.py`：实验、模型、组别、处理组和文档版本隔离；文档标识含正文哈希。对象携带原文引文、字符偏移、值和生成出处。规范化标签与严格整数/日期校验只提供机械 grounding，信任级别是 `GROUNDED`。
- `prefix.py`：冻结正常 parent 请求的完整 JSON，fork 仅追加 cracking 后缀。相同 scope 的 namespace 在最早 system 内容中，分支输入只使用当下实际文档查询历史。先持久化正常 parent 的响应，才准许派发 fork；不为 cracking 额外打开文档。
- `runner.py`：脚本化工具控制器，目录查询先于原文回退；用后台任务派生 fork。当前题起始快照屏蔽本题新对象，题末等待分支持久化后才允许下一题。T1 的预测由 mock 确定性输出已打开文档的两种属性。
- `store.py`：列表成员先全部校验，再在显式 `BEGIN IMMEDIATE` 中一次发布。任一成员无效或写入失败，整次发布回滚。读取不返回半列表，超过上限返回 `UNAVAILABLE`。同一对象重复产生会去重，相互冲突的来源不会强行合并。
- `cache_evidence.py` / `cache_review.py`：独立审计一对已提供的非流式记录；消息与全部其他参数分开哈希，保留未知与零，不驱动 runner。hash 不证明真实 token 前缀，合计仅为 completion-reported credits。
- `ledger.py`：同一个 SQLite 账本的所有组共用请求上限，每题共用 cracking 输出预算（默认 4096，含 reasoning）。状态为 `RESERVED → DISPATCHED → SETTLED`，派发意图先落盘；确定未派发的 reservation 可以续跑，已结算结果可直接复用。

测试覆盖 B0/T0/T1 的跨题 A→B 复用、未来题/gold DTO 隔离、每题新会话、组别/模型/实验/文档版本隔离、当前题快照、目录与回退顺序、精确 prefix、正常 parent 完成后 fork、列表完整读取与事务回滚、原子准入、去重及中断恢复。

原子列表测试使用独立 writer/reader 连接，检查事务确已开始与每连接外键开启。在 WAL 模式下，reader 在提交前和自己的旧事务内均看不到新对象；重新开启 reader 事务后才看见整个列表。

若进程在 `DISPATCHED` 后、结果持久化前中断，结果不确定，重新打开 runner 会标为 `UNKNOWN` 并停止该数据库中的新派发，跨组也不能重置。P0 不提供自动重试或外部请求对账；保留数据库和轨迹用于检查，不应删掉账本把它当作安全恢复。

## P0 单独运行的边界

这里没有真实模型决策、FanOutQA 数据/评分、真实 provider、tokenizer、缓存路由/TTL、队列延迟、TTFT/TTFP、计费回执或实测质量与收益。本地 prefix 一致性只验证结构前提。模拟异步任务不能证明缓存会保留到真实 fork 发出时。

列表只支持非空组；`model_declared_complete` 是模型声明，不证明语义上的完整性。grounding 校验引文和值，不能证明语义正确性或解决别名。失败候选会被拒绝且不影响原文回答；SQL、身份不一致、未知调用等基础设施错误仍停止实验。无需据此扩展到生产调度框架或付费适配器。

响应/usage 已有上述离线契约检查与测量草案，仍需审查、补齐真实传输与账户准入，再另行批准付费验证。P0 和这次离线契约工作都没有修改科学主实验 manifest、授权标记或产品运行路径。
