# CrackRAG

CrackRAG 是一个有证据约束的财务文档问答项目。首问查阅 PDF，独立后台流程抽取并验证候选；后续精确需求命中有效事实时，直接复用，不再调用模型。

首发目标为 **v0.1.0**。本分支仍在验收，最终结果以 [发布验收记录](docs/release-validation.md) 为准。

## 支持范围

- Linux amd64、CPU、Docker Compose；Windows 使用 Docker Desktop 的 Linux 容器。
- 默认 mock，不需供应商密钥或 BGE 权重；真实模式显式准备 DeepSeek 与本地 BGE-M3。
- 明确实体、年度、合并口径的营业收入、营业成本、净利润，以及原文披露或已有有效派生事实的毛利率。
- 来源需要可核验的文本层、实体、年份列、单位和口径。缺证据返回 `INCONCLUSIVE`；不支持的问题返回 `UNSUPPORTED`。
- 首发不提供开放式问答、OCR、复杂跨页表格推理和任意计算。未识别实体需通过经审查的词表变更支持。

## 本机启动

产品管理入口只需宿主安装 Docker 与 Docker Compose。源码可用 Git 获取，或解压发行包；开发及可选验收脚本另有测试依赖。首次构建会下载镜像和锁定的依赖；mock 不下载模型权重。

```sh
git clone https://github.com/XJfyrh/CrackRAG.git
cd CrackRAG
./crackrag init
./crackrag up
```

打开 <http://localhost:18086>，使用 `.release/secrets/access_token` 内本机随机令牌登录。初始化生成的 `.release/` 是私有目录，不提交、不复制到公开发行包。

Windows PowerShell 使用 `./crackrag.ps1` 执行相同子命令。需要已启动 Docker Desktop 并切换到 Linux 容器。

## 演示路线

1. 下载页面中的自制正例，上传 `financial.pdf`，解析第 1 页。
2. 选择该文档，查询“样例控股2024年营业收入是多少？”，先关闭事实构建。核对答案、原页和本次调用。
3. 再次查询并开启事实构建，选择 `COLD_ALLOWED`。独立后台候选通过验证后才进入正式事实；真实模式下会产生额外费用。
4. 待后台终态后重复该问题，检查有效事实复用和零模型调用。
5. 上传并**只选择** `ambiguous.pdf`，询问净利润。该样例缺少要求的口径附注，应解释证据不足。
6. 从“最近查询”切换历史、刷新页面，确认沿用原查询，无新收费请求。
7. 单独询问营业成本并开启构建，将后台策略改为“预计缓存可复用时构建”（`HOT_ONLY`）。观察实际后台终态；经验依据不足时可以跳过，不要求供应商缓存必然命中。默认 mock 的这一步仅演示策略流程。

受控恢复、备份和真实模式步骤见 [运行手册](docs/operations.md)。实际浏览器验收和演示视频将在发布记录中给出。

## 实现

[架构与信任边界](docs/architecture.md) · [开发与分支约定](docs/development.md) · [发布验收记录](docs/release-validation.md)

Go 负责鉴权、数据库事务、最终语义验证和答案生成。Python 负责解析、检索编排和模型交互。PostgreSQL 保存来源、候选、验证报告、有效事实、任务和费用；Redis 用于恢复通知。刷新页面或重连不会重新提交问题。

费用准入使用冷价上界，结算记录供应商实际 usage 的估算金额；未知费用保留占用并停止后续真实派发。缓存经验窗口只用于调度判断，不代表供应商承诺命中或缓存生命周期。

## 许可

自有源码与自制样例采用 [MIT](LICENSE)。依赖各自适用原许可，特别是 PyMuPDF/MuPDF 的 AGPL 或商业许可；详见 [第三方许可](THIRD_PARTY_NOTICES.md)。这里的 MIT 声明不覆盖完整依赖组合。真实发行人 PDF 不随仓库分发，使用官方链接和摘要复现。
