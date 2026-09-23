# 静态回放演示站

[打开演示](https://xjfyrh.github.io/CrackRAG/demo/) · [真实运行录像与核对记录](live-demo.md) · [发行版验收](release-validation.md)

演示站让读者无需安装 Docker，就能点开一次已记录的年报问答：查看回答和本次费用、打开原页，
接着看数字保存后如何供重复和改写提问复用。它与产品共用前端界面；浏览器内的回放层只返回
冻结快照，没有后端连接、模型请求或新费用。README 中的 30 秒动图录自这个回放站。

## 快照内容与边界

前四问取自 [v0.1.0 真实演示](live-demo.md) 的同一轮 DeepSeek 运行：

| 顺序 | 可见结果 | 真实模型调用 |
| --- | --- | ---: |
| 第一次问松霖科技 2024 年合并营业收入 | 有回答、引用和原页 | 1 |
| 显式保存同一数字，等待后台验证 | 保存为可复用事实 | 2 |
| 再问同一问题 | 直接复用 | 0 |
| 换一种问法 | 直接复用 | 0 |

这四问已知用量的费用估算合计 **¥0.00989692**，与
[公开的结构化记录](../eval/release/live-demo/record.json)一致；费用不是供应商账单结算额。
最后一问“样例控股 2024 年净利润是多少？”来自**单独录制的模拟模式**缺证据样例。
页面、动图均标明这一点，不把五问说成同一轮真实运行，也不把模拟调用算入真实费用。
这段演示只展示一家公司、一个财务需求，不是独立质量评测或通用降本证明。

回答引用的 `web/public/samples/demo-annual-report.pdf` 是松霖科技 2024 年年度报告的**原第 86 页单页摘录**，
用于在静态站内显示当时的证据区域。完整报告不在仓库中；[官方来源与 SHA-256](live-demo.md#来源与复现)
可供重新取得原文件。该页仍属发行人材料，不受本仓库 MIT 授权覆盖，见
[第三方说明](../THIRD_PARTY_NOTICES.md)。另两份自制 PDF 仅用于本地模拟演示。

快照文件是 [`web/src/demo/session.json`](../web/src/demo/session.json)。发布守卫
[`scripts/check_demo_session.py`](../scripts/check_demo_session.py) 核对五步顺序、回答状态、引用和禁止公开字段；
占位内容或不完整快照不能发布。原始恢复数据库、凭据、完整请求 trace 与私有账本不进入公开仓库。

## 本地检查

```sh
python scripts/check_demo_session.py web/src/demo/session.json
npm ci --prefix web
npm run build:demo --prefix web
cd web
npx vite preview --config vite.demo.config.ts --host 127.0.0.1 --port 18418
```

打开 <http://127.0.0.1:18418/CrackRAG/demo/>。演示站使用 `/CrackRAG/demo/` 基址；
正常产品构建仍使用 `/`。刷新和打开最近查询只重看已记录结果。

[`scripts/record_demo_gif.mjs`](../scripts/record_demo_gif.mjs) 在本地预览启动后录制 README 动图：

```sh
node scripts/record_demo_gif.mjs
```

录制脚本读取五步快照，逐步操作静态站，并将浏览器帧编码成
[`docs/media/demo-loop.gif`](media/demo-loop.gif)。它只读本地站点，不调用供应商。
录制工具的依赖锁定在 `web/package-lock.json` 的开发依赖中；`npm ci --prefix web` 后即可重录。

## 更新快照

只在发布前核对来源、费用和授权后更新。抓取脚本
[`scripts/capture_demo_replay.mjs`](../scripts/capture_demo_replay.mjs) 把原始运行记录先写在忽略的私有目录，
`--phase mock` 与 `--phase live` 分开执行；只有审查记录后明确运行 `--phase publish` 才改写公开快照。
每次真实请求仍必须通过产品的价格、余额、并发和未知费用保护；
这些保护失败时停止真实派发，不能为录制回放而跳过。当前快照依据已经验收的运行记录，
本次制作静态站没有新增付费请求。修改快照后，应重录动图、重新核对单页引用、运行发布守卫及浏览器检查。

GitHub Pages 由 [demo-site 工作流](../.github/workflows/demo-site.yml)从 `main` 的静态构建发布。
Pages 只部署 `web/dist-demo`，不部署 API 或数据库。正式版本 `v0.1.0` 的源码、录像和验收记录
由 GitHub Release 固定；后续对演示站的改动记录在 `main` 上。
