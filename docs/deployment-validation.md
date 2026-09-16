# 部署与恢复验证

本文记录 2026-09-16 的部署验证。修复前候选发布清单为 `d183620ebaebeba6e1cb7994faed9dc78c047017c47fad598b42f754aa4e00d6`，包含 402 个文件。后续真实质量检查发现同表换行的正例被拒绝，该候选不能作为最终发行身份。本文保留它已通过的部署检查与此前预发行构建的专项证据，不代替修复后的真实质量结论。故障演示均使用 mock；模型资产校验和 BGE CPU 推理不调用付费 API。

## 修复前候选构建已实测结果

| 项目 | 结果与范围 |
| --- | --- |
| 干净克隆与构建 | 从远程仓库干净克隆后构建 Linux CPU 镜像；Go 二进制、Go/Python/前端源码、协议、提示词、配置、迁移、前端产物及部署入口共 402 个文件纳入清单 |
| 本机启动 | PowerShell 的 Docker-only 入口完成独立项目初始化与启动，四个服务健康；只有 HTTP 绑定本机回环地址，数据库和内部 gRPC 未发布宿主端口 |
| 普通 HTTP 闭环 | 自制正例正常上传与解析；首问为 `SUPPORTED` 且不隐式发布事实；显式构建产生 `COMMITTED` 后台任务；再次查询为 `FULL`、0 次模型调用；缺附注样例为 `INCONCLUSIVE`；范围外问题为 `UNSUPPORTED`、0 次调用；来源 PDF、历史重读及幂等重放检查通过 |
| 候选持久化后故障 | 在干净项目中用普通接口生成候选，确认前台完成且调用均已结算后，终止主 API/runtime 并重启 Redis；副实例完成同一任务，候选摘要不变，调用数保持 2 → 2；后续 FULL 查询为 0 次模型调用，付费调用为 0 |
| 实际混组件拒绝 | 新 API 配前一预发行版本的完整 runtime 文件包；旧包自身清单校验通过，并通过 Docker 本地只读卷运行。API 的 `/healthz` 返回 HTTP 503、`RELEASE_INTEGRITY_FAILED`；移除覆盖并通过统一 `up` 恢复后返回 HTTP 200、修复前候选身份。全过程查询数、调用数与付费调用数均为 0 |

两组 API/runtime 的恢复演示共享 PostgreSQL、Redis、PDF 和账本；默认仍只启动一组。混组件检查证明实际运行组件不一致时健康门禁拒绝，不将普通连接超时算作版本校验成功。

修复前提交 `ef18357` 的 Linux 宿主完整 shell 流程及 29 张表的新空卷恢复已在 [GitHub Actions](https://github.com/XJfyrh/CrackRAG/actions/runs/35090376246) 全部通过，恢复摘要 SHA-256 为 `ffffdee3d2ae292d7f346eb8d43a7463834df18ab97d5480c6eea07b52eedbaa`。修复后的最终构建仍需重新验证。最终视频、独立真实质量评分和成本序列结论由 [发布验收记录](release-validation.md) 汇总；本文不将尚未完成的检查标为通过。

## 后续修复的入口隔离

Shell 与 PowerShell 管理入口均显式传入 `--project-name "$CRACKRAG_PROJECT"`，Go 集成检查固定传入 `--project-name crackrag-release-tests`。这修复了修复前候选只依赖 YAML `name` 时，宿主残留 `COMPOSE_PROJECT_NAME` 可以覆盖目标项目的问题，避免停止服务或测试清库操作选错项目。

`python scripts/release_entrypoints_test.py -v` 在 Windows 上 3 项全部通过：实际执行两种入口并捕获替代 Docker 的参数，覆盖默认/自定义项目与有/无 `compose.env` 共 8 种组合；另在替代 subprocess 中执行 Go 检查脚本，确认启动和全部测试库 DROP/CREATE 命令均绑定专用项目。测试没有连接 Docker daemon 或修改数据库。再在故意设置不同 `COMPOSE_PROJECT_NAME` 的环境执行实际 `docker compose config`，发布项目与测试项目仍分别解析为指定名称，发布 HTTP 仍只绑定 `127.0.0.1`。该配置检查没有创建或停止容器，也没有模型调用。修复后的完整发行构建及 CI 结果需另行记录。

## 此前预发行构建的专项证据

以下记录保留原验证范围，不表述为在修复前候选镜像上重新执行过。

| 项目 | 结果与范围 |
| --- | --- |
| 模型资产 | 从官方固定 revision 下载 10 个文件，尺寸与 SHA-256 全部通过；断网 CPU 推理输出 1024 维归一化向量 |
| 冷启动资源样本 | BGE 首次校验、加载及单条推理约 21.3 秒，进程峰值 RSS 约 1.82 GiB；mock 空闲四服务合计约 269 MiB。这是一次本机测量，不是容量或性能承诺 |
| 早期新空卷恢复 | 6 份文档、6 个版本、11 个查询、2 个后台任务、2 条事实和 9 条调用恢复后计数一致；事实与调用完整行序列摘要一致，6 份 PDF 的 SHA-256 一致；新凭证可读取恢复的文档与历史 |
| 前一构建的合成 UNKNOWN 与待恢复任务备份 | 独立 mock 项目通过正常接口生成候选后，停止应用，再在该专用库显式插入一条标记为合成的 UNKNOWN（占用 0.01836000 CNY）并让候选任务租约过期。统一 `backup` → 新空卷 `restore` 后，7 条调用、2 个任务、2 条候选、2 个 batch、3 个预算的完整行相等；启动后待处理任务进入 `OUTCOME_UNKNOWN`，调用数仍为 7、候选与费用占用不变 |
| Linux 测试 | 实际镜像中运行 Python 全套 201 项，201 通过、0 跳过；另运行管理工具 10 项测试全部通过，包括备份文件恢复与凭证隔离 |

合成 UNKNOWN 专项对应前一构建 `2197316ecfbc4380851be8b3b84f66f79261a2307c6db9b2e8ccbb49427a7d0d`（401 个文件）。逐文件清单比较确认，该构建到修复前候选的备份/恢复、预算准入和任务恢复核心源码没有变化；变更包括实体词表、公开配置中的历史说明清理、样例清单换行规范和新增恢复准入测试，Go 二进制及整体发布身份随之变化。早期 6 文档恢复对应更早的 `3d616091979a5bbf583c895363f9ddc7a99dd72d601e73052f850166323ff88c` 构建。历史证据不替代最终 Linux CI 的 29 表恢复检查。

## 准入保护与测试对应

| 边界 | 验证证据 |
| --- | --- |
| 价格过期、未来日期、费率变化、HTML 被修改 | `TestPriceMustBeDatedHashedAndWithinAuthorization` 拒绝这些输入；发布会话同时绑定价格 JSON/HTML 摘要及到期时间 |
| 暂停、会话过期、源码被修改、清单遗漏、局部超限、自动重试配置 | `TestReleaseManifestAndSessionFailClosed`；HTTP 派发前再次检查失败时，Python hook 测试证明没有进入传输层，且产生明确的零费用未派发结算 |
| 新 UNKNOWN | PostgreSQL 测试 `TestM3PostgresFinalBudgetUnknownAllowsLateSettlement` 验证停止新付费准入、保留占用，原请求的合法迟到结算仍可登记；恢复状态矩阵验证未知结果不会重新派发。新空卷恢复专项通过 `TestReleaseRestoredUnknownRejectsPaidAdmission` 在只读事务中调用生产准入函数，确认以 `GLOBAL_COST_UNKNOWN` 拒绝新付费准入 |
| 期初重复导入与预算并发 | `TestReleasePostgresOpeningAndAdmission` 验证幂等导入、禁止修改/删除、不同期初拒绝、未登记会话拒绝、并发局部限额以及期初占用参与累计 100 CNY 上限 |
| 组件身份 | 文件内容变化和未绑定源码有拒绝测试；修复前候选双实例实测清单一致。实际混装新 API 与旧 runtime 文件包时 HTTP 503 拒绝，恢复匹配版本后 HTTP 200；全过程未提交查询 |

## 结论的边界

基础 `pg_dump`/新空卷恢复覆盖已结算的 mock 账本；额外独立专项覆盖显式合成的 UNKNOWN 和持久候选待恢复状态。故障行从未发往供应商，0.01836000 CNY 是测试占用，不是本项目实际新增费用。付费拒绝验证调用生产准入函数，未发起真实模型请求，也不代表所有灾难恢复情形均已覆盖。

备份命令保存整个数据库，没有筛掉 UNKNOWN、预留、候选或待处理任务。恢复不会把未知费用改成零，也不会清除任务期限。启动后的任务能否继续，仍由原有租约、期限、配置、文档权限及费用状态决定；过期或撤权的任务可以保留为终态而不发布事实。Redis 不作为账本备份来源，持久权威状态保存在 PostgreSQL。

Linux 与 PowerShell 调用相同的 Compose、容器管理代码和服务端校验。PowerShell 使用 `docker cp` 传输数据库二进制备份，Linux 使用 shell 字节流。Windows PowerShell 完整管理流程及修复前候选干净克隆的 mock/恢复演示已实跑；此前 Linux 镜像测试也已通过。修复前提交的 Linux 完整 CLI 与 29 表恢复另有上述 CI 实测；修复后的最终构建需要自己的结果，不能由历史或 Windows 测试代替。

## 合成故障专项的复核边界

正常恢复演示用 `./crackrag demo-recovery`，不修改业务表。上述 UNKNOWN 专项是另一项离线故障注入检查：只使用独立 Compose 项目、新数据库和新卷；先停止 API/runtime，再给该库插入带 `synthetic_fixture=release-backup-unknown-v1` 标记的 UNKNOWN，并保留正常接口产生的候选。不能在演示主库或真实账本上运行故障注入。

备份与恢复均通过统一管理入口完成。恢复前后比较调用、任务、候选、batch 和预算完整行；启动后再次比较，确认未知费用不变、原任务不能重新派发。`TestReleaseRestoredUnknownRejectsPaidAdmission` 仅在显式设置 `RELEASE_RESTORE_DATABASE_URL` 时读取此类恢复库，默认跳过；它要求专用合成标记、固定占用及 UNKNOWN 状态，并使用只读事务，既不准备价格或真实会话，也不构造供应商传输对象。
