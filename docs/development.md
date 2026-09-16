# 持续开发与发行约定

公开仓库是唯一持续开发主线。`main` 保存可复现代码；探索工作使用 `XJfyrh/experiment-<topic>`，产品改动使用 `XJfyrh/<topic>`，验收后的正式版本使用不可移动的 `v<major>.<minor>.<patch>` 标签。

旧研究 Git 历史、冻结证据和工作区独有材料保存在独立私有只读归档。`baseline-import.json` 记录干净基线的来源和排除项；它不是新版本的最终发布哈希或完整历史冻结。公开仓库不会伪造缺失的历史材料来使旧审计通过。

产品默认 `financial-supported-v1` 且仅开放 M3 入口。研究人员可显式选择 `CRACKRAG_ANSWER_POLICY=legacy` 运行历史研究路径；此配置不属于首发产品验收。

## 检查

```sh
python scripts/check_public_source.py
python scripts/check_python.py
cd api && go test ./...
cd ../web && npm ci && npm run build
```

Python 运行时依赖在 `ai-runtime/requirements-m1.lock`，Go/npm 使用各自 lockfile。名称中的 M1/M2/M3/M4 是实现演进来源，非不同公开产品。

不设置 PostgreSQL/Redis 测试地址会跳过相应集成检查；**跳过不代表通过**。CI 和发行验收必须使用隔离测试数据库，不能指向开发或真实账本。测试辅助配置在 `tests/compose.yaml`。

完整 Go 集成检查使用仓库根目录的 `python scripts/check_go.py`，需要宿主 Go、Python 和 Docker。入口仅重建固定的专用测试库，并将旧生命周期测试的七个结算样例分别隔离到新库/进程，保留所有断言且不重试；详情见 [验收记录](release-validation.md)。直接向同一个测试库执行原始 `go test` 时，旧夹具清理与后台恢复可能竞争，不作为本版完整验收入口。

真实验证仅由维护者显式执行，不能放进自动 CI。未来问题、金标与人工评分不能进入模型或抽取提示。修复中使用过的真实样本必须转为回归集，不得继续称独立样本。

发布顺序：冻结范围与用例 → 核对公开内容 → 核心/数据库/浏览器/mock 检查 → 显式真实评测 → 从拟公开提交干净克隆复现 → 记录提交和产物 SHA-256 → 标签与 Release。报告只写实际完成结果。
