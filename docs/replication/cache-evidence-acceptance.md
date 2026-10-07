# 缓存探针对离线审计验收

**2026-10-07，本轮离线范围完成；完整 M1 和真实 M2 尚未完成。** 本轮没有真实模型调用、密钥读取、FanOutQA 取数、金额预留/结算或产品路径改动。

## 基线与交付

PR [#2](https://github.com/XJfyrh/CrackRAG/pull/2) 已于 2026-10-07 合并。已通过 GitHub 元数据和本地 fetch 确认目标研究分支的基线为 `f8ef226ffcc932d6d2c8240bd826f36ecaeb4fa8`，包含前轮 head `de47f0b17f77eb2447aa0918ca558d294177cd33`；二者源码树相同。

本轮分支 `XJfyrh/experiment-adc-cache-evidence` 从该合并基线建立，不重写已有分支。新增：

1. `research/adc/cache_evidence.py`：完整请求/消息/参数哈希、parent/fork 与 generation ID 关联、记录时序、未知字段及 reported credits 合计
2. `python -B -m research.adc.cache_review`：16 组合成案例，完整可复核输入、期望结果和审计问题
3. 29 项新测试与[审计口径](cache-evidence-v1.md)，更新研究入口及 M1 实现清单

没有建立新运行配置；复用 `RouteContract` 与现有响应解析器。P0 mock 账本、科学协议和预算授权保持原状。

## 实际检查

| 检查 | 结果 |
| --- | --- |
| 修改前 ADC 基线 | 58 / 58 通过 |
| 最终 ADC 子集 | 87 / 87 通过；新增 29 项 |
| 缓存探针对合成报告 | 16 / 16 符合预期；包括原始合成输入与审计 |
| 既有 provider 合成报告 | 10 / 10 符合预期 |
| P0 新库运行及同库续跑 | 两次 stdout 报告逐字节一致，无重复派发 |
| `git diff --check` | 通过 |
| 公开源码扫描 | 0 findings；启发式扫描不证明绝无敏感内容 |
| 科学协议 JSON | 与合并基线逐字节相同 |
| 公共 Python 总入口 | **未通过**：执行 190 项，175 通过，15 项 import errors，0 skipped |

总入口是 `python -B scripts/check_python.py`。15 项失败全部为本环境缺少 `google`（protobuf）或 `grpc`模块的导入错误；没有为了通过而安装/替换锁定依赖或跳过这些检查。ADC 子集不是产品全量通过的替代。Go、数据库、Redis、Docker 和产品浏览器检查本轮未执行；真实 provider、付费缓存、试点与主实验也未执行。

固定协议 `eval/replication/fanoutqa-v1/protocol.json` 的 SHA-256：`905b8a9e5fe3c3c2059ace8ee8a5e0b476fbaa365400abe7d5ff3ae0cca96002`。

独立代码复核指出并已修复：parent link 缺失不能当显式 null；JSON decoder 可接受的孤立 Unicode surrogate 在生成 UTF-8 hash 时不能崩溃。对应回归覆盖缺失 parent link，以及 request/response/generation metadata 三处非法 Unicode。

## 复核命令

```sh
python -B -m unittest discover -s research/adc/tests -v
python -B -m research.adc.cache_review
python -B -m research.adc.provider_review
python -B scripts/check_public_source.py
python -B scripts/check_python.py
```

最后一条需准备产品锁定依赖；以上“未通过”是本地真实运行结果，不以未执行的远端 CI 替代。报告只保存手写合成材料，真实响应与账本不得提交公开仓库。

## 下一关口

先审查本轮离线边界与检查口径，再推进 M1 余项：真实账户账本的离线状态机、语料工具/通用代理、评分适配与依赖。`consistent` 只表示输入记录符合本地检查，任何合成 cached tokens 都不是供应商实测；endpoint/服务层、tokenizer 与真实账户对账仍需未来 M2 单独验收和批准。
