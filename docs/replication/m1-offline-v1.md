# M1 离线闭环：运行、隔离与恢复

2026-10-07。M1 的**离线工程闭环**已实现，等待维护者验收；M2 真实供应商验证、M3 相关题生成/人工核验、M4 judge 校准仍未完成。这里没有 live transport、凭证读取、付费请求或 FanOutQA 主实验结果。科学 manifest 仍为 PLANNED，未改变题目选集或授权。

## 运行与独立评分

基础闭环只需要 Python 3.11+ 标准库；参考验证环境为 CPython 3.12。输出目录必须在仓库外：

```sh
python -B -m research.adc.m1 --run-dir /tmp/crackrag-m1
python -B -m research.adc.grade \
  --answers /tmp/crackrag-m1/T1-answers.json --fixture-gold \
  --diagnostic-normalizer --synthetic-judge \
  --account-path /tmp/crackrag-m1/account.sqlite3 \
  --output /tmp/crackrag-m1/T1-evaluation.json
python -B -m unittest discover -s research/adc/tests -v
```

第一条命令运行自制 Aster/Beryl 两属性语料。模型决策来自 `offline_fixture.py` 的确定性本地策略，通过与代理分离的 `FakeTransport` 返回 OpenRouter 形状的合成响应。通用控制器不硬编码主体、关系、答案或文档遍历顺序。三臂共用同一工具 schema、上下文规则与步数限制；区别是 B0 不读跨题对象、不 cracking，T0/T1 的抽取范围分别为当前/推测关系。

| 组 | R/Q 回答调用 | R/Q cracking 调用 | Q 原文打开 | Q 对象读取 |
| --- | --- | --- | --- | --- |
| B0 | 4 / 4 | 0 / 0 | 2 | 0 |
| T0 | 4 / 4 | 2 / 2 | 2 | 0 |
| T1 | 4 / 4 | 2 / 0 | 0 | 2 |

总计 30 次**合成调用**，全部经统一账户；独立评分命令再增加 2 次合成 judge。此 judge 固定返回 C，仅验证评分与记账连接，不测量质量。相同输入/数据库再运行不派发新调用。R/Q 答案分别为 `Aster: 10; Beryl: 20` 和 `Aster: 4; Beryl: 7`；不能解释为论文准确率、真实缓存或降本结论。

### 真正的字符串评分依赖

简化 normalizer 的分数只标 `diagnostic`，`acc=null`。要启用 pinned FanOutQA 字符串算法及真实 ftfy/spaCy lemmatization，显式建立独立环境：

```sh
python3.12 -m venv /tmp/crackrag-scoring
/tmp/crackrag-scoring/bin/python -m pip install -r research/adc/requirements-scoring.lock
/tmp/crackrag-scoring/bin/python -B -m research.adc.grade \
  --answers /tmp/crackrag-m1/T1-answers.json --fixture-gold \
  --output /tmp/crackrag-m1/T1-string-evaluation.json
```

锁文件固定完整已验证环境与官方 `en_core_web_sm-3.7.1` wheel 哈希。参考核心版本为 ftfy 6.1.3 / spaCy 3.7.2 / model 3.7.1；缺失或版本不符返回 unavailable/null，不下载模型、不回退冒充官方分数。已安装模型的评分阶段无网络需求。上述自制夹具的 genuine `acc.loose/strict` 均为 1；未配置 judge 时 judge 值为 null、missing=2。

适配源码来自 FanOutQA `989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33`。保留递归字符串评分、答案渲染、A–F rubric、4000 字符截断及末字符解析；B/C/E 映射为 1，A/D/F 为 0。源码摘要在 `evaluation.py`，MIT 通知保留。没有导入上游会初始化引擎的 llm/scorer，也不调用 BLEURT/ROUGE。真实 judge 的选择和校准仍属于 M4。

## API 前的真实格式接线

默认 M1 和 synthetic judge 已通过 `providers/http_boundary.py`：OpenRouter 原生 `max_tokens`、固定 `provider.order`、`allow_fallbacks=false`、非流式请求，先纯 preflight 校验，再统一预留/派发意图，最后把相同 canonical payload 编成 HTTP body。没有在 ledger hash 之后偷偷改参数。

`PreparedRequest` 固定 chat/completions URL、方法、限额和无效 redacted Authorization 占位，`executable=false`；只允许 `FakeHTTPExchange` 返回 bytes。没有网络 client、socket、真实 Bearer 参数或环境变量读取。返回 body 有大小/depth/UTF-8/重复键/数值精度/content-type/状态/超时等边界检查；坏响应经原账户 UNKNOWN 停派，确定本地无效的请求则在预留之前拒绝。敏感回显不会写入 raw bytes 日志。

原 provider JSON 不被注入自定义字段。wire 请求/响应哈希、有限原始 HTTP bytes、状态和解码失败信息另存 `transport_evidence`；已有账户数据库只新增可空证据列，不重置 account UUID/费用。报告只显示 wire 摘要，完整 bytes 留在私有账户。HTTP 字节 roundtrip 是合成测试，不等于实际 endpoint/provider 服务层验收。

### 本地 MediaWiki envelopes → 真 converter → 同一代理

新增 `mediawiki.py` 准备与校验三类真实格式请求：截止 epoch 最新修订、指定 oldid 的 HTML parse、当前 top-10 搜索。仅导入本地响应，校验 request/pageid/revid/timestamp/HTTP/error/warnings；search snippets 不交给模型。HTML converter 精确适配 pinned FanOutQA，已与原纯函数对照；不是一个手写近似 Markdown 转换器。

可从全新输出目录运行完整导入链：

```sh
python3.12 -m venv /tmp/crackrag-corpus
/tmp/crackrag-corpus/bin/python -m pip install -r research/adc/requirements-corpus.lock
/tmp/crackrag-corpus/bin/python -B -m research.adc.m1 \
  --run-dir /tmp/crackrag-preapi --import-fixture
/tmp/crackrag-corpus/bin/python -B -m research.adc.m1 \
  --run-dir /tmp/crackrag-preapi --import-fixture
```

这两次使用自制 HTML/API envelopes：首次30次合成调用、全部具有 wire evidence；第二次仍30。若已有本地 captured/synthetic bundle：

```sh
/tmp/crackrag-corpus/bin/python -B -m research.adc.mediawiki \
  --bundle /path/to/local-bundle.json --output-dir /tmp/imported-corpus
```

bundle schema 位于 `import_bundle` 文档字符串；输出是可直接供 `OfflineCorpus.from_manifest` 使用的 manifest。Markdown、acquisition sidecar、import seal 以临时文件/fsync/排他原子发布写入，manifest 最后可见；中断可续导入，不覆盖不一致既有文件。POSIX 还同步父目录；Windows 保留文件 fsync 和排他原子发布，不执行不受支持的目录 fsync。没有下载 Wikipedia 正文或获取当前搜索；`network_verified=false`、`latest_revision_observed=false` 始终显式保留。`oldid` 的 parse 仍受上游模板/transclusion/rendering 语义限制，不能据此声称每项模板事实都是历史快照。

## 数据与工具边界

- `corpus.py` 接受明确提供的本地解析 Markdown、pageid/revid、修订/取得时间、来源、parser version 与 SHA-256。拒绝 epoch 之后的修订；没有声称仅凭时间戳就证明“截止时最新修订”。上述 importer 已做 pinned 转换语义离线对照；实际 MediaWiki 取得与真实 corpus 证据仍须验证。
- 正文 lossless 按空白段落边界、Unicode 字符数分页；默认超大单段拒绝整页，或显式 `keep_whole` 并记录超限。每个 part 独立文档键/证据位置。`open` 返回完整 part 及前后分页指针，不按问题过滤正文。
- 搜索只重放规范化 query 的首条已冻结记录，最多 10 个标题/pageid/链接；不暴露 snippets、gold 页清单或正文缓存命中标记。缺失记录显式报错，不虚构搜索结果。治疗臂额外附带可见 snapshot 中的目录；缺对象仍可原文回退。
- `search/open/catalogue/read_objects/notes_read/notes_write/close` 通过严格参数 allowlist 执行，无任意 SQL/shell。基础工具表相同；每响应最多一个工具调用。notes 仅当题；close 替换当题原文上下文并记录操作。
- `CurrentQuestion.view()` 仅 id/text。调度器的 RQSequence 持久化 workload 哈希，R 的所有 forks 完成后才开放 Q。R/Q 会话、笔记、回答不继承；仅历史文档查询和已发布对象跨题。
- 每题开始固定对象 snapshot；本题新对象不能帮助本题。open 当下的历史单独冻结并持久化，重启不得让后来 read_objects 污染早先 fork suffix。
- `AgentPolicy` 冻结最大步数、字符上下文上限、输出上限及对象读取上限；回答请求与追加指令/历史后的完整 cracking 请求均检查字符上限。超限 fork 在账户预留前停止，记录 `fork_context_limit`，题目最终为 `context_limit` 未答，不压缩治疗臂或丢弃分母。CLI 默认是离线验收版本，不是 M4/M5 已冻结科学参数。

可提供 `--corpus-manifest`、`--questions`（恰好两条 id/text，先 R 后 Q）、`--transport-scripts` 运行另一组离线 replay，三项必须一起给出。脚本目录含 `B0.json/T0.json/T1.json`，每个是 `{canonical_request_sha256: synthetic_response}` 映射；拒绝位置数组，避免重启跳过已结算请求后脚本错位。manifest 的 cache_path 必须在其 cache root 内。代理进程不读取 gold；FanOutQA 拆分工具仅把投影交给代理，证据分解不会进入 corpus 导航。

## 单账户与对象事务

`accounting.py` 的独立 SQLite 是所有 answer/cracking/generator/judge/probe、模型、实验和对象库的**唯一准入权威**。每个账户持久 UUID，不能通过换对象 store 重置额度；每个对象 store 的 run manifest 也绑定此 UUID，封存前就拒绝更换账户。多个实验必须传同一 `--account-path`；评分密封件绑定账户 ID，并拒绝无绑定、不同账户或新账户。复制/篡改数据库不是受支持的重置办法。

金额按精确 Decimal 保存为 **completion-reported credits**，不暗当 USD。默认离线额度 1 credit、200 请求、4096 cracking 输出 tokens/题；单调用预留 0.01 credit、512 输出 tokens，仅是可测试上界。冷价/tokenizer、价格档、credits/USD/最终平台计费关系要靠 M2 冻结。状态与历史总占用跨重启持久化：

1. `RESERVED` 原子预留金额、请求数、cracking 输出上限后才可 dispatch；只有一个 claimant
2. `DISPATCHED` 意图先落盘；`SETTLED` 按已知 reported cost/usage 幂等结算、释放多余预留
3. 超时、崩溃、缺失/空 usage 或 cost、路由不符、超出上界变为 `UNKNOWN`；保留预留并停止所有角色/新 store
4. 独占控制器重启时 `recover_inflight()` 将遗留 DISPATCHED 标 UNKNOWN；并发读/预留连接不会擅自把活跃请求作废
5. 维护者调用 `reconcile(attempt, evidence={source,reference}, amount, completion_tokens, response_evidence=...)`，再显式 `resume(evidence=...)`；不能自动重试 UNKNOWN

金额对账和响应恢复分开。恢复的完整 envelope 必须匹配原模型/路由和明确的费用/输出量，另存于 recovered 字段，不覆盖原始缺失证据。有费用而没有可用响应时，续跑把该回答标为 `response_unavailable` 或拒绝该 fork；确定未派发的 RELEASED 同样不自动再派发。超出账户总上限不能 resume。恢复函数是离线工具接口，操作示例和测试不能授权真实平台账单修改。

对象 Store 中的 `call_ledger` 只是已结算账户调用的投影，为原子发布外键服务，不是第二个预算。金额 USD 列保持 null。候选仍经原 grounding/整组原子性/去重规则；没有半个列表发布。模型输出坏但已知费用仍结算；基础设施或 store 不变量错误停止运行。

CLI 对共用 account path 持有 OS advisory owner lock，避免两个控制器交叉恢复；底层 SQLite 另有并发准入测试。库使用者必须遵守同样单控制器约定。

## 封存与报告

输出目录包含共用账户、三份对象 SQLite、每臂答案 seal、store seal、report 和 summary。答案 seal 绑定账户 ID、顺序 question manifest 和 workload hash；独立评估前核对同 ID 的 gold question 文字，避免拿另一题同 ID 错评。建议跨信任边界保留 `--expected-sha256`。哈希是完整性校验，不是签名或来源证明。

输出采用临时文件、fsync 和原子 replace；已存在答案/store seal 必须完全一致。评分拒绝覆盖答案、gold、account 或其他非评分产物，包括解析路径/硬链接别名。未答/失败保留分母；未执行和未取得的 judge 分数保持 unknown，而非上游“无 key 默认 0”。不同 judge 模型使用不同 attempt 身份、共享账户。

每臂保留 `target_Q`（含 Q 自己 cracking）和 `sequence_R_Q`（含 R 建库）成本口径，并另外汇总 all-role 账户。token 分解费用、reported credits、平台对账费用分开，后两种真实证据未取得时为 null。缓存探针对审计连接实际合成 transport 留档；只有相同持久 clock domain 的 monotonic 时间才能比较 parent 完成与 fork 派发，跨进程/补录响应的未知时序不能伪造。

## 下一道真实验证边界

离线 fake transport、typed response、ledger、loop、seal、scorer 已连接。请求构造、wire 编解码与本地语料导入已经完成，不再因阶段标签推迟这些可离线工作。下一阶段需要单独批准的 M2：在真实网络/凭证边界验证 HTTP/endpoint/服务层和无 fallback 路由、账户权限、tokenizer/schema/tool/reasoning 参数、cache read/write usage、TTFT/时序、冷价上界与账单对账、真实 corpus 取得证据。当前故意不带 HTTP/credential 实现，不能通过传入一个 duck-typed live transport 越过边界。

M3 的 210 道 R 生成与人工核验、M4 人工标签/judge 校准、M5 运行和 M6 科学统计均未执行。未改变产品运行路径、旧财务账本或科学协议授权字段。
