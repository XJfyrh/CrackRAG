# Provider 响应契约：离线检查版

**状态：离线实现，2026-10-07；不是 M2 实测。** 已实现非流式响应的结构检查、usage 归一化与返回身份比较。没有网络传输、SDK、密钥读取、真实请求或真实费用结算。所有演示输入都是手写合成夹具。

## 快速使用

从仓库根目录运行，Python 3.11+，无需安装依赖：

```sh
python -B -m research.adc.provider_review
python -B -m unittest discover -s research/adc/tests -v
```

第一条只把 10 组合成案例的检查报告写到 stdout。`all_expected=true` 表示解析结果符合夹具预期，不代表供应商行为通过验收。它不接受密钥、模型地址或待调用模型参数。

研究入口：[research/adc/README.md](../../research/adc/README.md)。未来真实缓存测试的准备见[最小测量方案](cache-measurement-v1.md)。

## 四个检查结果必须分开

| 输出 | 含义 | 不能推导什么 |
| --- | --- | --- |
| `usable_output` | 支持范围内，完整 assistant 文本或 function 调用结构可读 | 答案正确、工具有权限、满足应用 schema、允许发布研究对象 |
| `usage.accounting_known` | 必需的 prompt/completion/total/cost 字段都有有效值 | 可选计费字段也有效、已对账、费用可结算 |
| `usage.accounting_valid` | 已识别并归一化的 usage 字段没有检测到非法值或一致性错误 | 缺失字段已知、金额已获账户证实 |
| `route_status` | `matched` / `mismatch` / `unverified`：返回模型与 provider 名称是否符合传入约束 | 指定地区、endpoint 变体、服务层或禁 fallback 已得到完整验证 |

`matched` 只是返回字段匹配。请求里写 `provider.order`、`only` 或 `allow_fallbacks=false`，不会被当成执行证据。这里没有付费运行准入函数；未来适配器还需同时检查预算、授权、endpoint 证据、响应问题与账本状态。

## 接口

`research.adc.providers` 导出 `normalize_response`、`RouteContract` 和结果类型。输入是已经解码的 JSON；函数不读取文件、不发请求、不执行工具。

```python
from research.adc.providers import RouteContract, normalize_response

result = normalize_response(
    response,
    RouteContract(
        expected_reported_model="synthetic/model-v1",
        allowed_providers=("Synthetic Provider",),
    ),
    generation_metadata=None,
    http_status=200,
)
```

示例标识是夹具标签，不是可调用的模型配置。`raw_response`、`raw_generation_metadata` 和 `usage.raw` 保留只读副本；`issues` 给出代码、路径与说明。不可变映射不能直接交给 `dataclasses.asdict` 深拷贝；使用演示报告或显式选择结果字段。

### usage

| 原字段 | 归一化字段 |
| --- | --- |
| `usage.prompt_tokens` | `prompt_tokens` |
| `usage.completion_tokens` | `completion_tokens` |
| `usage.total_tokens` | `total_tokens` |
| `usage.prompt_tokens_details.cached_tokens` | `cached_tokens` |
| `usage.prompt_tokens_details.cache_write_tokens` | `cache_write_tokens` |
| `usage.completion_tokens_details.reasoning_tokens` | `reasoning_tokens` |
| `usage.cost` | 十进制定点字符串 `cost`，`cost_unit="credits"` |

每个字段的 `states` 区分 `known`、`missing`、`null`、`invalid`。合法的零不丢弃；缺失或 null 不补零。Token 必须为非负整数，布尔值不能冒充整数；cost 必须可解析为有限非负数。为避免异常指数展开耗尽内存，cost 输入最多 256 字符、128 位有效系数、指数绝对值不超过 128、归一化结果不超过 256 字符，越界记为 invalid。小数文本按十进制保留；若调用者此前已用浮点解码并丢失精度，解析器不能恢复丢掉的位。

reasoning 已是 completion 的子集，不再重复相加。检查总量及缓存/推理子集的一致性；异常字段单列，不用猜测值修补。现阶段保留未知原字段，不归一化所有未来供应商扩展。

OpenRouter 文档把 completion 的 `usage.cost` 描述为 credits，generation 的 `total_cost` 描述为 USD。本模块不执行汇率或 credits→USD 转换，不把上游推理成本、缓存折扣与账户费用加在一起；generation 费用只保存在原始元数据，不覆盖 completion usage。

### 响应与工具

只支持一个非流式 assistant choice、文本和 function tool calls。检查 HTTP 状态、顶层/choice/message 错误、结束原因、函数名称和 ID、参数 JSON 对象及重复键。`length`、`error`、`content_filter`、未知或空结束原因不作为完整输出。

工具参数解析成功只证明 JSON 结构完整。调用权限、工具 schema 和执行仍由后续代理层负责；本模块绝不执行工具。过深的工具参数 JSON 会产生 `invalid_tool_call`，不向外泄漏递归异常。流式 `delta`、多 choice、多模态内容和显式 `tool_calls: null` 不在当前支持范围。拒绝语义、工具 schema、完整原始 payload 的总体大小/深度预算尚未实现；`message.refusal` 等未知字段仅保留在原始副本中，不能据此当作面向不可信网络输入的完整网关。

### generation 与路由

可选 `generation_metadata` 接受离线的 `{"data": ...}` 对象。只在 `data.id` 与 completion `id` 完全相同时使用其返回模型/provider_name；错误关联不补全身份，原始证据保留。互相矛盾的模型或 provider 返回值判定为 mismatch。

provider 展示名不证明精确 endpoint。`openrouter_metadata`、fallback 尝试、服务层、地区、`system_fingerprint`、上游 ID 与 generation 金额仍在原始副本中，不是本版全部可执行判定。M2 必须先补齐需要的支持矩阵，不能拿本模块的 `matched` 代替路由验收。

## 与 P0 的关系

现有 `MockProvider`、`Ledger`、`runner` 与原有演示命令不变：仍只接收本地 mock。新模块没有被接入派发或结算路径。科学协议的 `execution_ready`、付费批准与冻结项未因这项离线工作改变。

后续顺序：审查这份契约及合成案例 → 完成真实账本/传输适配与离线恢复验证 → 冻结最小探针 → 单独批准 M2 → 用真实返回补充经过审查的契约案例。不要把手写夹具标为录制响应。

## 官方来源

查阅日期：2026-10-07。这些是字段设计依据，不是账户验收证据。

- [Completion API](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion)
- [Usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting)：当前说明 usage 自动返回；参考 schema 仍允许缺省，因此代码保留 unknown
- [Generation metadata](https://openrouter.ai/docs/api/api-reference/generations/get-request-&-usage-metadata-for-a-generation)
- [Provider routing](https://openrouter.ai/docs/guides/routing/provider-selection)
- [Router metadata](https://openrouter.ai/docs/guides/features/router-metadata)
- [Errors and debugging](https://openrouter.ai/docs/api_reference/errors-and-debugging)
