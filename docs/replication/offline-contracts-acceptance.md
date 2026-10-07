# 离线 provider 契约验收记录

**2026-10-07，已完成本次离线范围，待维护者验收。** 本次不是完整 M1 或 M2。没有请求真实模型、访问密钥、采集 FanOutQA 正文或修改产品运行路径。

## 本次交付

1. `research/adc/providers`：非流式响应结构检查、usage 归一化、返回模型/provider 名称比较、generation 身份关联
2. `python -B -m research.adc.provider_review`：10 组合成案例，覆盖已知零、缺失/null、非法 token、路由证据缺口、模型变化、截断工具参数与 HTTP 200 内错误
3. [响应契约](provider-contract-v1.md)与[缓存测量草案](cache-measurement-v1.md)，并统一研究入口、路线、开发计划和协议说明

基线：`2c67fd2986b999ad8003d30e6292b8fc0a0f79df`，来自 `XJfyrh/experiment-adc-replication`。工作分支：`XJfyrh/experiment-adc-offline-contracts`。

本地交付包代码阶段提交：`79001239248c245e7d79d8a75c83b38957e3ce1f`（`feat(adc): 新增离线供应商响应契约检查`）。本地交付包文档阶段为 `748da83aa3031989adedb750095773b3807d8059`。

随后按维护者要求通过 GitHub 发布同名研究分支。GitHub 代码阶段提交为 `70fbcb499a9099d4d3281a3cb7d04b7db1a2ac8c`，源码树与本地代码阶段相同；提交身份和时间元数据不同，因此提交号不同。GitHub 文档阶段仅额外补充本段映射及发布状态，交付包原提交保持可恢复。

## 如何验收

只需要 Python 3.11+ 的标准库，从仓库根目录执行：

```sh
python -B -m unittest discover -s research/adc/tests -v
python -B -m research.adc.provider_review
python -B scripts/check_public_source.py
```

期望 ADC 共 58 项测试通过，包含原 P0 27 项、provider 28 项、报告入口 3 项。报告应有 `measurement_kind=synthetic_contract_checks`、`all_expected=true`、`live_requests=0`、`paid_calls_authorized=false`。这些字段说明本地检查性质，不是供应商计费证明。

复查 P0 续跑时，使用一个新的、仓库外的输出目录：

```sh
python -B -m research.adc --run-dir /tmp/crackrag-adc-review
python -B -m research.adc --run-dir /tmp/crackrag-adc-review
```

两次应读取同一份数据库，报告保持一致，不重复派发。演示会写 SQLite/WAL/SHM 与 `summary.json`；这些运行产物不要放进公开源码。若该目录已有不同输入的实验，按新的目录名重新做这项本地验收，不删除未知账本以绕过恢复限制。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 修改前 P0 测试 | 27 / 27 通过 |
| 修改后 ADC 子集 | 58 / 58 通过，独立复核也通过 |
| P0 首次演示及同库再次运行 | 22 次 mock 调用；两次报告逐字节一致 |
| 加入新模块后同库续跑 | 与修改前报告逐字节一致 |
| 合成 provider 案例 | 10 / 10 符合预期；无网络请求 |
| `git diff --check` | 通过 |
| 公开源码扫描 | 通过，0 findings；仅启发式检查，不证明绝无敏感内容 |
| 付费科学协议 JSON | 与基线逐字节一致 |

协议文件：`eval/replication/fanoutqa-v1/protocol.json`，SHA-256：`905b8a9e5fe3c3c2059ace8ee8a5e0b476fbaa365400abe7d5ff3ae0cca96002`。未改变模型选择、样本数、预算、阶段授权或主实验可执行状态。

复核中修复了两个异常输入问题：超大十进制指数在格式化前被界限检查拦截；过深工具参数 JSON 转为明确解析问题，不再泄漏递归异常。范围限制详见契约文档，不将离线解析器当作完整网络网关。

## 没有执行的检查

- 产品 Python 全量回归：未执行，本环境未准备完整锁定依赖
- Go/数据库/Redis/Docker 集成与产品浏览器验收：未执行，本环境未准备所需工具与服务
- 真实 provider、真实缓存、质量/成本试点与主实验：未执行，当前没有真实传输实现或本阶段付费批准
- 验收当时未执行 GitHub 推送；随后按维护者要求将两个阶段发布到独立研究分支。未创建 PR、合并或发行版本；交付包保留原本地提交

## 验收后的下一步

先决定本接口约束和测量方案是否接受，再安排真实传输/账户账本及语料工具。只有新增离线代码通过，不足以开启真实 M2。协议中的整体 M1、数据准备、端点验收、付费门禁、人工校准和主实验冻结仍需分别完成。
