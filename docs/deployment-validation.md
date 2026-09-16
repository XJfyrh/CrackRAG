# 部署与恢复验证

2026-09-16，最终 v0.1.0 产品清单为 `37f2a29509b2e5fd14f70659036009619c83cf3dbec1116d9b1638789601478d`（404 文件）。以下区分最终构建实测与较早构建的专项检查；质量和费用结果见 [验收记录](release-validation.md)。

## 最终构建实测

| 项目 | 结果与范围 |
| --- | --- |
| 干净启动 | 从 GitHub 获取源码，在新目录、新凭证、新数据库和新持久卷运行 Linux amd64 CPU mock；四个服务健康，仅 HTTP 绑定 localhost |
| 问答与复用 | 正常上传、解析和首问为 SUPPORTED，不隐式发布事实；显式构建完成后 FULL 复用零模型调用；缺证据为 INCONCLUSIVE，范围外问题为 UNSUPPORTED |
| 浏览器 | 来源 PDF、历史切换、刷新重连、后台终态及恢复结果查看通过；实际录像 4 分 21.44 秒，使用 mock 与自制样例 |
| 服务中断 | 候选持久化后终止主 API/runtime 并重启 Redis；副实例完成同一任务，候选摘要不变，调用 2→2，后续 FULL 零调用 |
| 新空卷恢复 | 统一 CLI 备份并恢复到新项目与空卷；29 张表的完整行摘要、原 PDF、历史答案及账本一致，恢复后 FULL 零调用 |
| 混合组件 | 最终 API 与旧 `2197316e…` runtime 实际混装；旧 runtime 自检通过，但 API 返回 HTTP 503、RELEASE_INTEGRITY_FAILED；恢复匹配组件后 HTTP 200。查询 12→12、调用 12→12、付费 0→0 |
| 恢复库只读准入 | 最终代码对已恢复的合成 UNKNOWN 专项库执行准入检查，返回 GLOBAL_COST_UNKNOWN；7 条调用及任务、候选、batch、预算完整行不变，0.01836000 CNY 合成占用保留 |

[最终 Linux CI](https://github.com/XJfyrh/CrackRAG/actions/runs/35097182002) 覆盖构建、数据库集成、mock、浏览器、中断恢复及新空卷恢复。PowerShell 管理入口也在 Docker Desktop Linux 容器中实跑。源码身份、视频及附件 SHA-256 见 Release；本机实测资源见 [资源与限制](release-validation.md#资源与限制)。

默认仅启动一组 API/runtime，恢复演示按需启动副实例；两组共享 PostgreSQL、Redis、PDF 和账本。管理命令显式指定 Compose 项目，测试清库命令固定使用专用测试项目，宿主残留的 COMPOSE_PROJECT_NAME 不改变目标。

## 较早构建的专项证据

以下证据保留其原构建身份，不冒称全部在最终镜像上重新执行。

| 专项 | 原身份、结果及界限 |
| --- | --- |
| 模型资产与冷加载 | 相同固定 revision 的 BGE-M3 10 个文件通过尺寸和 SHA-256 校验；断网 CPU 推理输出 1024 维归一化向量。一次冷加载约 21.3 秒，峰值 RSS 约 1.82 GiB，是资源样本，不是容量承诺 |
| 合成 UNKNOWN 与待恢复任务 | 在 `2197316ecfbc4380851be8b3b84f66f79261a2307c6db9b2e8ccbb49427a7d0d` 构建的独立 mock 库中，正常接口生成候选后停止应用，注入明确标记的合成 UNKNOWN 并使任务租约过期；经统一 backup/restore 到新空卷后，7 条调用、2 个任务、2 条候选、2 个 batch、3 个预算完整行相等。启动后任务为 OUTCOME_UNKNOWN，调用、候选和费用不变 |

合成 UNKNOWN 专项的完整备份恢复发生在上述旧构建；最终代码另行通过的是恢复库只读准入复核。最终构建的 29 表新空卷检查覆盖正常 mock 账本。这两类证据互相补充，不能替代彼此。

## 准入保护与测试对应

| 边界 | 验证证据 |
| --- | --- |
| 价格过期、未来日期、费率或 HTML 被修改 | TestPriceMustBeDatedHashedAndWithinAuthorization；发布会话绑定价格摘要与到期时间 |
| 暂停、会话过期、清单遗漏或源码变更、局部超限、自动重试配置 | TestReleaseManifestAndSessionFailClosed；Python 派发 hook 检查失败时不进入传输层，并作明确的零费用未派发结算 |
| UNKNOWN | TestM3PostgresFinalBudgetUnknownAllowsLateSettlement 保留占用并停止新派发，允许原请求合法迟到结算；TestReleaseRestoredUnknownRejectsPaidAdmission 在恢复库只读事务中验证 GLOBAL_COST_UNKNOWN |
| 期初导入与并发预算 | TestReleasePostgresOpeningAndAdmission 验证幂等、禁止篡改、不同期初拒绝、会话登记及局部/累计上限 |
| 组件身份 | 文件篡改和清单缺项回归，加上最终 API/旧 runtime 实际混装的 HTTP 503 拒绝检查 |

## 复核方式与边界

正常恢复演示使用 `./crackrag demo-recovery`，备份恢复检查见 [运行手册](operations.md)。它们通过产品接口生成样例和候选，不制造未知的真实计费结果。

合成 UNKNOWN 专项只在独立 Compose 项目、新数据库和新卷内进行：停止 API/runtime 后注入带 `synthetic_fixture=release-backup-unknown-v1` 标记的故障行。故障行从未发往供应商，0.01836000 CNY 是测试占用。不能在演示主库或真实账本上执行故障注入。

TestReleaseRestoredUnknownRejectsPaidAdmission 仅在显式设置 RELEASE_RESTORE_DATABASE_URL 时运行，默认跳过。它检查专用合成标记、固定占用和 UNKNOWN 状态，使用只读事务，不准备真实会话或发起模型请求。

备份保存整个数据库，包括 UNKNOWN、预留、候选和待处理任务；恢复不清零费用或重置期限。任务能否继续取决于原租约、期限、配置、文档权限和费用状态。PostgreSQL 是持久状态的权威来源，Redis 仅用于通知。本次检查不代表所有灾难恢复场景均已覆盖。
