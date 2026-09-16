# 本机运行手册

本文对应源码构建的 Linux amd64 / CPU 部署。运行结果与资源测量见 [验收记录](release-validation.md)。以下用 Linux `./crackrag` 表示统一入口；Windows PowerShell 改为 `./crackrag.ps1`，其余子命令相同。

## 初始化与日常管理

```sh
./crackrag init
./crackrag up
./crackrag status
./crackrag logs api
./crackrag logs runtime
./crackrag stop
```

`init` 构建镜像并生成随机本机凭证。`up` 验证构建清单，启动单 API、单 runtime、PostgreSQL 和 Redis。只有 HTTP 绑定 `127.0.0.1:18086`，数据库和内部 gRPC 不暴露宿主端口。浏览器令牌保存在 `.release/secrets/access_token`；密钥和配置只读挂载给应用。

停止服务保留持久卷。请勿删除卷以“修复”费用或 UNKNOWN。`CRACKRAG_PROJECT` 可指定以 `crackrag-release` 开头的独立项目名；`CRACKRAG_STATE` 指定其私有本机目录；`CRACKRAG_PORT` 改 HTTP 端口。这些环境变量应在所有管理命令中保持一致。

## 显式真实模式

真实模式使用服务器端 DeepSeek 密钥、本地 BGE-M3 和新鲜官方价格。模型准备和价格核验不产生付费模型请求。

```sh
./crackrag models
./crackrag price refresh
```

将供应商密钥写入私有 `.release/secrets/deepseek_api_key`，不写入命令参数、公开报告或前端。初始化的 `.env.example` 仅供历史研究入口参考，产品 Compose 从私有文件读取。

新使用者为自己的**全新项目**创建明确的零余额起点：

```sh
./crackrag opening-new --project-id my-local-project
```

已有费用的项目必须导入经核对的历史 opening 文件。它包含项目身份、已知估算金额、保留占用、来源 SHA-256、截至时间及旧付费实例已停止的确认；未知历史占用需要原授权引用，不能静默当作零。本仓库不附带维护者私有账本。

停止所有旧付费实例，确认只保留一个活跃真实账本，然后：

```sh
./crackrag live prepare --opening /release/opening.json --confirm-exclusive --cap 5 --requests 80
./crackrag live enable
./crackrag status
```

`prepare` 绑定构建、资产、价格、opening 和局部限额，保持暂停；`enable` 在服务就绪后启用。默认会话 5 CNY / 80 次，仍受累计 100 CNY 上限及全局并发控制。每次付费派发前重新检查授权与账本，按冷价保守预留；无自动重试。费用没有 usage 时保留占用并停止后续真实调用。

```sh
./crackrag live pause
./crackrag mock
```

暂停阻止新付费派发，已发出的请求可能仍结算。`mock` 切换回模拟运行，不清除历史账本。价格过期时重新刷新并准备新的会话，不能只修改时间戳。构建变更后应暂停并重建；旧 live session 不能绑定新镜像继续派发。

## 备份与新空卷恢复

```sh
./crackrag backup rehearsal
```

备份先暂停付费、停止 API/runtime，保存数据库、PDF、账本及状态配置，并记录文件 SHA-256。文件位于 `.release/backups/rehearsal`，属于私有材料。凭证不包括在备份中；重新初始化会生成新凭证。备份后应用保持停止。价格与会话仍有效时可用 `up` 恢复服务，付费仍暂停；若已过期，先 `mock` 恢复读取，或刷新价格并重新准备真实会话。

恢复使用**新的项目名和私有目录**，避免覆盖旧卷。例如 Linux：

```sh
export CRACKRAG_PROJECT=crackrag-release-restore
export CRACKRAG_STATE="$PWD/.release/restore-target"
export CRACKRAG_PORT=18088
./crackrag init
# 将原备份 rehearsal 完整复制到新目录的 backups/rehearsal。
./crackrag restore rehearsal
./crackrag up
```

Windows 对应 `$env:CRACKRAG_PROJECT`、`$env:CRACKRAG_STATE`、`$env:CRACKRAG_PORT`。恢复拒绝非空数据库、非空 PDF 目标、哈希不符或不同发布身份。恢复后保持 mock/暂停，核对文档、来源、事实、任务终态、UNKNOWN 和累计费用后，才重新准备真实会话。模型权重不包含在备份中，真实运行前需执行 `models` 重新准备与校验。重新准备会话时用 `--opening /release/state/opening-balance.json` 延续恢复的期初记录，不能创建新的零余额起点。不能同时启用恢复副本与原付费实例。

Linux 可在完成源码镜像构建后运行独立的零付费备份恢复检查：

```sh
sh scripts/check_restore_mock.sh
```

检查创建两个新的 mock Compose 项目及 `.release/restore-check-*` 私有目录，通过正常 HTTP 上传、查询和构建事实，然后使用统一 CLI 备份、恢复到新空卷。恢复应用启动前，它核对 29 张文档、证据、事实、候选、验证报告、后台任务、投递审计及账本相关表的行数和全行内容摘要；启动后核对来源 PDF 原字节、历史答案和 FULL 零调用复用。默认只用 localhost 19286 / 19287；端口占用时可传 `--source-port` 和 `--target-port`。结束后停止两个新项目，保留私有备份和卷；摘要写入 `tmp/restore-mock-acceptance.json`。脚本拒绝在 Windows 运行，也不会操作已有部署。此检查使用 mock 数据，不制造或结算真实 UNKNOWN 请求。

## 演示恢复的边界

普通页面可以重新打开后台任务并继续验证已保存候选；刷新与历史切换都读取同一个 Run。候选保存后中断、重启、Redis 通知丢失的专项检查使用隔离测试环境，并记录是否重复抽取/发布或丢失费用。生产真实模式禁止 mock 故障注入，不依赖手工修改数据库演示。

初始化后的本机环境可运行完整的零付费故障演示：

```sh
./crackrag demo-recovery
```

Windows 使用 `./crackrag.ps1 demo-recovery`。命令会切换到 mock/暂停，启动恢复副实例，通过正常 HTTP 上传自制样例并构建事实。主 runtime 在候选持久化后暂停；确认调用都已结算后，仅终止该 Compose 项目的主 API/runtime 并重启 Redis。副实例必须完成同一任务、保持候选摘要与调用集合一致，随后 FULL 查询必须为零模型调用。命令输出 PASS 及本机证据位置；结束时恢复主实例的普通 mock 配置并停止副实例。

它会短暂停止本机服务，适合演示和发布前复核。已实测结果与尚未覆盖的恢复场景见 [部署与恢复验证](deployment-validation.md)。本演示没有制造未知的真实计费结果，不证明供应商缓存写入时刻或缓存精确覆盖范围。

## 显式升级与恢复副本

`models --offline --smoke` 在断网资产上执行零付费 Embedding 烟测。源码/词表升级后，先暂停，再 `build`；需要变更活动配置时使用 `activate`，它验证当前发布身份并确认没有活动任务或未决调用，保留历史事实、契约和账本。

词表升级时，新的镜像需先启动一次以登记配置，再显式激活。例如保持零付费的操作顺序为 `live pause` → `build` → `mock` → `activate` → `up`。激活不等于把旧事实自动认定为符合新规则；它只切换后续工作的活动配置，保留历史记录。

`recovery up|status|stop` 管理可选第二组 API/runtime，默认不启动，HTTP 端口 18087。两组共享同一数据库、PDF 和累计账本，付费并发槽不会加倍。此副本只用于恢复验收；备份、停止和真实准备会同时停止两组应用。
