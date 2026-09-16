# Holdout v3：首发小规模独立验收准备包

状态：**PREPARED_NOT_EXECUTED**，`no_api_calls: true`。这是固定输入与人工金标，不是通过报告。v1 福耀/安井和 v2 龙盛/恒立已转回归，首次失败及成本结果保留不变。

`code-freeze.json` 先冻结语义实现；选材只看官方原页和 140 DPI Poppler 渲染，不用生产 parser、validator 或模型试金标。松霖是冻结后首次发现的来源；玲珑曾在 v2 准备时仅作网页文字预筛备用，当时未下载、目检、冻结金标、运行或用于修复。本次按“未用于修复调试”的门槛纳入，公开披露这段准备期接触，**不声称此前从未见过它**。两份来源均未用于产品修改，选材不是随机抽样。

## 官方来源和原页

- [松霖科技 2024 年报](https://static.cninfo.com.cn/finalpage/2025-03-31/1222962236.PDF)：4,366,562 字节，209 页，SHA256 `e3fa31c2e40941bb0ecc0f921deddc01d3e0ff3dbeed7a4a4434492330a4902e`。正例原物理页 **86**，负例 **87**；印刷页码相同。
- [玲珑轮胎 2024 年报](https://static.cninfo.com.cn/finalpage/2025-04-25/1223285950.PDF)：1,933,526 字节，234 页，SHA256 `ee5e21ef24837cb7b4d806c79dab28b18588bb73f52ee43b05ca68fd04d27172`。正例原物理页 **100**，负例 **101**；印刷页码相同。

每个 scope 只复制一个原页，选页 PDF 的物理页均为 1。禁止补标题、重排表格、OCR、跨页推断或借另一张表的口径。原始 PDF、完整抽取和图片只存公共仓库外。

## 人工金标（2024 年、合并报告口径、人民币元）

| 发行人 | 营业收入 | 营业成本 | 总净利润 | 原页 |
|---|---:|---:|---:|---:|
| 松霖科技 | 3,014,989,619.04 | 1,948,457,767.50 | 446,415,013.59 | 86 |
| 玲珑轮胎 | 22,057,938,923 | 17,191,782,641 | 1,752,035,423 | 100 |

完整标量金标和 2023 比较列见 `gold.json`。净利润指总净利润，不能替代为归母净利润；营业成本不能替代为营业总成本。使用 Decimal 精确比较，只允许千位符和末尾零格式差异。

## 12 项质量检查

唯一模型请求文件是 `model-inputs.json`，仅传 question/request 和动态授权文件 ID。`gold.json`、计划和本说明只供评估，不能进 prompt、工具或上传内容。

- 六个支持正例：每发行人收入、成本、净利润；每项独立冷租户，收入 seed 允许正常构建。
- 两个缺证据负例：松霖只上传原页 87（虽重复列年，但无收入行、合并标题和单位）；玲珑只上传原页 101（上方续表缺合并收入，下方母公司收入不得替代）。询问 2024 合并营业收入。
- 两个不支持口径负例：松霖调整后营业收入、玲珑归母净利润；不得静默换成其他指标。
- 两个复用例：仅复用本发行人收入 seed，分别原问与改写。等待 seed 正确且正常构建实际发布后再运行，否则 BLOCKED，不算通过。

正例弃答是失败，负例 crash/timeout 不是通过；分别报告 6 / 4 / 2，全部 12 项通过才满足这一小规模门槛。若用本来源指导语义、parser 或 prompt 修复，必须转回归并保留首轮结果，不能修后重跑改称独立验收。

## 16 问短序列

两发行人各 baseline/build 两臂，每臂新租户按收入、成本、重复收入、收入改写四问。共 4 个序列 context，加 10 个质量 context，合计 14 个隔离 context。baseline 为 strict M3 + `build_facts=false`，build 为同策略 + `true`。实际输入顺序为松霖 baseline→build，玲珑 build→baseline；不能只在计划里声明顺序。

同一实例/项目账本、模型、配置、价格快照、PDF 和选页；逐问等待全部前后台任务结算。累计成本包含构建、重试和延迟工作，未知 usage 保留未知与占用；只在两臂答案正确且已结算时比较成本，允许成本上升。两发行人短测不支持普遍降本或供应商缓存精确写入时刻、覆盖范围、TTL 的结论。总支出仍受人民币 100 元项目上限约束，真实执行前由执行负责人绑定剩余额度及运行身份。

## 下载与复现

仓库根目录运行，宿主需 Python 3.11+、`pypdf==6.8.0`，渲染另需 Poppler；这些是验证准备依赖，不是 Docker 产品启动依赖。

```powershell
python scripts/release_prepare_quality.py --manifest eval/release/quality/holdout-v3/sources.json --destination ../CrackRAG-private-validation/holdout-v3 --render --report ../CrackRAG-private-validation/holdout-v3/prepared-reproduction.json
```

`--pdftoppm` 可指定渲染器路径，`--verify-existing` 离线核验。脚本仅读来源清单，无模型请求能力；核验原始 bytes/SHA256/页数、选页 content stream/抽取文本/mediabox 一致，输出选页 bytes/hash/page_map 供 live driver 的 `--preparation-report` 使用。文件、抽取和图片留在仓库外。原报告上限 20 MiB、每 scope 至多 32 页，遇不一致或不同内容覆盖即拒绝。

首次真实执行前还需绑定最终 commit、配置/价格、计划哈希和账本余额。本准备包不含运行通过或降本结论。
