# M1 离线闭环验收记录

2026-10-07。实现与离线检查完成，待维护者验收。代码阶段提交：`8593d49`（账户/语料/评分）、`ad12e6d`（通用代理/独立命令/恢复回归）、`c79a720`（封存前对象库到账户绑定）、`538d9e0`（API 前 HTTP 字节边界与真正语料导入链）。本页不是 M2 真实 API 通过证明、付费授权或论文结果报告。运行说明见 [M1 离线闭环](m1-offline-v1.md)。

## 本次补齐

- provider-driven 的统一工具/上下文循环，三臂差异只限对象复用与 cracking 范围；fixture 决策位于独立 fake transport
- dated Markdown manifest、搜索 replay、lossless 段落分页和完整 part 标识
- 所有角色/模型/对象 store 共用的金额预留、请求/输出预算、UNKNOWN、显式对账与响应恢复；持久 account UUID
- 只含当前 id/text 的代理 DTO、R→Q 屏障、每题 snapshot、冻结 open 历史、原子完整列表与去重
- 账户/问题 manifest 绑定的答案 seal，独立评估进程，精确 pinned FanOutQA 算法/rubric 和独立完整 scorer 依赖锁
- 双成本口径、raw evidence、缓存探针对审计与跨进程时钟域限制；未取得值不补零
- 原生 OpenRouter 请求/序列化 fake HTTP exchange 与独立 wire evidence；MediaWiki request-bound envelopes、pinned HTML converter、atomic importer 与完整三臂连接

## 最终验证

| 检查 | 结果 | 范围 |
| --- | --- | --- |
| 安装 corpus lock 后的 ADC suite | **233 passed，0 skipped** | P0、provider、cache、M1 与 genuine importer 全部 ADC 测试 |
| 基础环境 ADC suite | **229 passed，4 skipped** | 4 个真正 converter 测试需要 corpus lock，跳过不算通过 |
| `scripts/check_python.py` | **474 passed，0 skipped** | 全仓 Python 入口，使用下述隔离测试环境 |
| 真进程中断矩阵 | **32 个 os._exit 子场景**通过 | 上述 ADC 测试中的两个用例；10 reserve、10 settle、2 publish、10 dispatch |
| M1 三臂 CLI 两次同库运行 | JSON 完全一致；**30→30** 调用 | 同库重放，无新派发 |
| `--import-fixture` 从全新目录执行两次 | JSON 完全一致；**30→30**；30份 wire evidence | genuine HTML converter→corpus→三臂→HTTP byte roundtrip→账户 |
| HTTP 边界专项 | **30 passed** | 精确字节/预检/迁移/超时/重复键/精度/UTF-8 与保留证据 |
| MediaWiki importer 专项 | **16 passed** | corpus lock 环境；另10个 HTML 案例与 pinned converter 一致 |
| 独立 synthetic judge | **30→32**；重复仍32 | 所有角色同账户，固定 C 明确标 synthetic |
| pinned 真字符串 normalizer | 自制两题 `acc.loose=1`、`acc.strict=1` | ftfy6.1.3/spaCy3.7.2/model3.7.1；无 judge 时 value=null、missing=2 |
| 独立 scoring 环境 `pip check` | 无依赖冲突 | 完整版本锁与官方模型 wheel 哈希 |
| pinned 源码 differential | 32 string、8 render、9 real-normalizer 案例及 prompt 一致 | 从核对 Git blob 的源码提取纯函数，没有导入上游模型引擎 |
| P0 CLI 两次同库运行 | JSON 完全一致 | 保留历史 P0 文档/request 字节格式与幂等性 |
| provider review / cache review | 10 / 16 组合成案例 `all_expected=true` | 既有契约与缓存证据审计未回退 |
| `scripts/check_public_source.py` | 0 findings | 启发式扫描，不宣称绝对无秘密 |
| `scripts/release_entrypoints_test.py -v` | 3 passed，1 skipped | PowerShell unavailable，未计为通过 |
| `git diff --check` | 通过 | 补丁格式 |

全仓测试环境是在现有宿主依赖基础上建立的独立 virtualenv，补装仓库已锁的 grpcio 1.83.1 / protobuf 7.36.1 / typing-extensions 4.16.0，再安装4包 `requirements-corpus.lock`，并设置子进程需要的 `PYTHONPATH="$PWD/ai-runtime/src:$PWD"`。这修复了旧环境因缺 protobuf/grpc 导致的导入错误；未将它们标为跳过。没有声称在该环境重新安装并验证了产品全部依赖锁。独立 scorer 和 corpus 环境分别由 `requirements-scoring.lock` / `requirements-corpus.lock` 对应的完整干净环境安装并验证；两者的 `pip check` 均通过。

没有运行新的 Go、Docker/PostgreSQL/Redis 或浏览器部署验收；没有修改这些产品路径，也没有用上述 Python 结果替代它们。托管 CI 状态由实际提交的 CI 另行确认。

## 重要故障回归

32 个真进程中断场景逐点验证：恢复后 T1 总请求仍恰为10、两次 publication、两题完成、Q 正确；UNKNOWN 会阻止另一 experiment/arm/group/对象库发请求。补录响应另存，不改写原始 raw 缺失状态。无可用响应的已对账调用成为明确失败/拒绝，不自动重试。

独立集成审查发现并已修复：

1. 恢复 parent 响应后 report 仍读取空原始 raw；现在使用单独补录 evidence，未知完成时间仍为 unknown
2. 重启时后来文档查询污染早期 fork suffix；现在读取当时持久化的 open-history
3. 位置型 synthetic 脚本在跳过已结算调用后错位；CLI 改为 request-hash 绑定
4. 多 judge 模型共享同一 attempt ID；现按 artifact 与模型隔离，并仍归同账户
5. 评分输出覆盖 sealed 输入/账户、另建账户绕开历史预算、同 ID 换题干误评；现提前拒绝路径/硬链接别名、绑定账户与完整 question manifest
6. 封存前给既有对象 store 换一个账户会重置准入历史；现在 run manifest 同样绑定持久 account UUID，构造代理时即拒绝
7. 合法 JSON 转义出的孤立 Unicode surrogate 会在 SQLite 前失败且丢失 wire 留档；现在 decoder 在返回可用响应之前校验严格 UTF-8，可疑响应进入 UNKNOWN 并保留原始 bytes/hash

## 解释边界

当前结果仅证明离线工程闭环与恢复机制。自制夹具的30次调用、0 reported credits、cache token 字段及固定 C judge 全部是合成值；不能作为真实质量、缓存或成本收益证据。字符串适配使用真正的 pinned normalizer，但这里评分对象仍是自制样例。

语料 importer 已验证 pinned 转换语义和 request/pageid/revid 绑定；但提供的 envelope 仍是 supplied/synthetic，不证明真实取得、截止时最新修订或模板事实都是历史快照。HTTP 原生请求/字节编解码/记账与 importer 所需离线接线已完成。真正的正文/搜索网络取得和 provider HTTP、endpoint/服务层、tokenizer、reasoning、缓存、价格上界、账户/账单对账属于下一道 M2 验证；当前请求准备物明确 nonexecutable，禁止 live transport/凭证访问。M3 相关题生成核验、M4 judge 校准、M5 主实验和 M6 统计均未完成。

科学协议仍 PLANNED，210题选集、执行/付费授权、产品运行路径和旧财务账本没有更改。原缓存/provider 阶段的验收页保留为历史记录，以本页与 M1 使用说明描述当前工程状态。
