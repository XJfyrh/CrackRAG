# 部署与恢复验证

本文记录 2026-09-16 的预发行部署验证，不代替最终版本的真实文档质量结论。所有故障演示使用 mock；模型权重校验和 BGE CPU 推理不调用付费 API。

## 已实测结果

| 项目 | 结果与范围 |
| --- | --- |
| 源码构建 | Linux CPU 镜像构建成功；Go 二进制、Go/Python/前端源码、协议、提示词、配置、迁移、前端产物及部署入口共 401 个文件纳入清单 |
| 本机启动 | PowerShell 的 Docker-only 入口完成初始化、启动、状态查询与停止；四个服务健康，只有 HTTP 绑定本机回环地址 |
| 可选恢复副实例 | 两组 API/runtime 识别同一发布清单，共享 PostgreSQL、Redis、PDF 和账本；默认只启动一组 |
| 模型资产 | 从官方固定 revision 下载 10 个文件，尺寸与 SHA-256 全部通过；断网 CPU 推理输出 1024 维归一化向量 |
| 冷启动资源样本 | BGE 首次校验、加载及单条推理约 21.3 秒，进程峰值 RSS 约 1.82 GiB；mock 空闲四服务合计约 269 MiB。这是一次本机测量，不是容量或性能承诺 |
| 新空卷恢复 | 6 份文档、6 个版本、11 个查询、2 个后台任务、2 条事实和 9 条调用恢复后计数一致；事实与调用完整行序列摘要一致，6 份 PDF 的 SHA-256 一致；新凭证可读取恢复的文档与历史 |
| 候选持久化后故障 | 通过普通上传和查询接口生成候选，确认前台完成且调用均已结算后，终止主 API/runtime 并重启 Redis；副实例完成同一任务，候选摘要不变，物理模型调用数保持 2 → 2；后续 FULL 查询为 0 次模型调用 |
| 合成 UNKNOWN 与待恢复任务备份 | 独立 mock 项目通过正常接口生成候选后，停止应用，再在该专用库显式插入一条标记为合成的 UNKNOWN（占用 0.01836000 CNY）并让候选任务租约过期。统一 `backup` → 新空卷 `restore` 后，7 条调用、2 个任务、2 条候选、2 个 batch、3 个预算的完整行相等；启动后待处理任务进入 `OUTCOME_UNKNOWN`，调用数仍为 7、候选与费用占用不变 |
| Linux 测试 | 实际镜像中运行 Python 全套 201 项，201 通过、0 跳过；另运行管理工具 10 项测试全部通过，包括备份文件恢复与凭证隔离 |

本轮部署清单摘要为 `2197316ecfbc4380851be8b3b84f66f79261a2307c6db9b2e8ccbb49427a7d0d`。后续源码或词表变更会产生新的清单；正式版本应在最终构建上重新确认关键流程。

## 准入保护与测试对应

| 边界 | 验证证据 |
| --- | --- |
| 价格过期、未来日期、费率变化、HTML 被修改 | `TestPriceMustBeDatedHashedAndWithinAuthorization` 拒绝这些输入；发布会话同时绑定价格 JSON/HTML 摘要及到期时间 |
| 暂停、会话过期、源码被修改、清单遗漏、局部超限、自动重试配置 | `TestReleaseManifestAndSessionFailClosed`；HTTP 派发前再次检查失败时，Python hook 测试证明没有进入传输层，且产生明确的零费用未派发结算 |
| 新 UNKNOWN | PostgreSQL 测试 `TestM3PostgresFinalBudgetUnknownAllowsLateSettlement` 验证停止新付费准入、保留占用，原请求的合法迟到结算仍可登记；恢复状态矩阵验证未知结果不会重新派发。新空卷恢复专项通过 `TestReleaseRestoredUnknownRejectsPaidAdmission` 在只读事务中调用生产准入函数，确认以 `GLOBAL_COST_UNKNOWN` 拒绝新付费准入 |
| 期初重复导入与预算并发 | `TestReleasePostgresOpeningAndAdmission` 验证幂等导入、禁止修改/删除、不同期初拒绝、未登记会话拒绝、并发局部限额以及期初占用参与累计 100 CNY 上限 |
| 组件身份 | 文件内容变化和未绑定源码有拒绝测试；双实例实测清单一致。API 还比较 Python 健康响应的清单身份，当前没有额外启动一套故意混装组件的容器演练 |

## 结论的边界

基础 `pg_dump`/新空卷恢复覆盖已结算的 mock 账本；额外独立专项覆盖显式合成的 UNKNOWN 和持久候选待恢复状态。故障行从未发往供应商，0.01836000 CNY 是测试占用，不是本项目实际新增费用。付费拒绝验证调用生产准入函数，未发起真实模型请求，也不代表所有灾难恢复情形均已覆盖。

备份命令保存整个数据库，没有筛掉 UNKNOWN、预留、候选或待处理任务。恢复不会把未知费用改成零，也不会清除任务期限。启动后的任务能否继续，仍由原有租约、期限、配置、文档权限及费用状态决定；过期或撤权的任务可以保留为终态而不发布事实。Redis 不作为账本备份来源，持久权威状态保存在 PostgreSQL。

Linux 与 PowerShell 调用相同的 Compose、容器管理代码和服务端校验。PowerShell 使用 `docker cp` 传输数据库二进制备份，Linux 使用 shell 字节流。本轮完整管理流程在 Windows PowerShell 上实跑；Linux 脚本通过语法与行为复核，Linux 镜像测试已实跑，但尚未把全部管理命令在独立 Linux 宿主上再演练一遍。

## 合成故障专项的复核边界

正常恢复演示用 `./crackrag demo-recovery`，不修改业务表。上述 UNKNOWN 专项是另一项离线故障注入检查：只使用独立 Compose 项目、新数据库和新卷；先停止 API/runtime，再给该库插入带 `synthetic_fixture=release-backup-unknown-v1` 标记的 UNKNOWN，并保留正常接口产生的候选。不能在演示主库或真实账本上运行故障注入。

备份与恢复均通过统一管理入口完成。恢复前后比较调用、任务、候选、batch 和预算完整行；启动后再次比较，确认未知费用不变、原任务不能重新派发。`TestReleaseRestoredUnknownRejectsPaidAdmission` 仅在显式设置 `RELEASE_RESTORE_DATABASE_URL` 时读取此类恢复库，默认跳过；它要求专用合成标记、固定占用及 UNKNOWN 状态，并使用只读事务，既不准备价格或真实会话，也不构造供应商传输对象。
