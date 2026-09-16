# Short-sequence model cost measurement

独立审查已核对全部 16 条序列答案、引用及正式事实，见 [质量报告](quality-results-v3.md)。基线 **0.02575216 CNY**，构建臂 **0.02354064 CNY**，本次累计估算减少 **8.59%**；后续 4 个重复/改写问题均为 FULL、零模型调用。这是两个来源的有限实测。此前另一批序列曾增加 16.65%，[原结果](cost-results-v1.md)完整保留。

Known, settled model-cost estimates in CNY. Supplier billing is not confirmed; answer correctness was not independently evaluated by this script.

Release manifest SHA-256: `37f2a29509b2e5fd14f70659036009619c83cf3dbec1116d9b1638789601478d`.
Frozen model inputs SHA-256: `81edf8885feadc46e7cbf750acb619cb44ffc341404c78db2d143faf4c74f060`.
Summarizer SHA-256: `bc77d50b0f0027eb5a28078e9806faef2606a9c904cc6180b9ad0f013dc9e387`.

| Case | State | Answer | Coverage | Calls | Estimated CNY | Zero-call validated reuse | Background terminals |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| s-songlin-baseline-1 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00809300 | False | none |
| s-songlin-baseline-2 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00198944 | False | none |
| s-songlin-baseline-3 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00094292 | False | none |
| s-songlin-baseline-4 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00195044 | False | none |
| s-songlin-build-1 | COMPLETED | SUPPORTED | MISSING | 2 | 0.00898144 | False | COMMITTED: 1 |
| s-songlin-build-2 | COMPLETED | SUPPORTED | MISSING | 2 | 0.00289588 | False | COMMITTED: 1 |
| s-songlin-build-3 | COMPLETED | SUPPORTED | FULL | 0 | 0 | True | none |
| s-songlin-build-4 | COMPLETED | SUPPORTED | FULL | 0 | 0 | True | none |
| s-linglong-baseline-1 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00808200 | False | none |
| s-linglong-baseline-2 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00194844 | False | none |
| s-linglong-baseline-3 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00080648 | False | none |
| s-linglong-baseline-4 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00193944 | False | none |
| s-linglong-build-1 | COMPLETED | SUPPORTED | MISSING | 2 | 0.00889344 | False | COMMITTED: 1 |
| s-linglong-build-2 | COMPLETED | SUPPORTED | MISSING | 2 | 0.00276988 | False | COMMITTED: 1 |
| s-linglong-build-3 | COMPLETED | SUPPORTED | FULL | 0 | 0 | True | none |
| s-linglong-build-4 | COMPLETED | SUPPORTED | FULL | 0 | 0 | True | none |

| Pair / arm | Stage | Calls | Estimated CNY | Failed HTTP calls |
| --- | --- | ---: | ---: | ---: |
| 1 / baseline | answer | 4 | 0.01297580 | 0 |
| 1 / baseline | extraction | 0 | 0 | 0 |
| 1 / baseline | probe | 0 | 0 | 0 |
| 1 / baseline | other | 0 | 0 | 0 |
| 1 / build | answer | 2 | 0.01005044 | 0 |
| 1 / build | extraction | 2 | 0.00182688 | 0 |
| 1 / build | probe | 0 | 0 | 0 |
| 1 / build | other | 0 | 0 | 0 |
| 2 / baseline | answer | 4 | 0.01277636 | 0 |
| 2 / baseline | extraction | 0 | 0 | 0 |
| 2 / baseline | probe | 0 | 0 | 0 |
| 2 / baseline | other | 0 | 0 | 0 |
| 2 / build | answer | 2 | 0.00995044 | 0 |
| 2 / build | extraction | 2 | 0.00171288 | 0 |
| 2 / build | probe | 0 | 0 | 0 |
| 2 / build | other | 0 | 0 | 0 |

Usage cells show the reported total / number of attempts missing that field. Missing counts are never inferred as zero.

| Pair / arm / stage | Input | Output | Total | Cache hit | Cache miss |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 / baseline / answer | 30147 / 0 | 568 / 0 | 30715 / 0 | 19840 / 0 | 10307 / 0 |
| 1 / baseline / extraction | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 1 / baseline / probe | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 1 / baseline / other | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 1 / build / answer | 15061 / 0 | 284 / 0 | 15345 / 0 | 6272 / 0 | 8789 / 0 |
| 1 / build / extraction | 13112 / 0 | 252 / 0 | 13364 / 0 | 12544 / 0 | 568 / 0 |
| 1 / build / probe | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 1 / build / other | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / baseline / answer | 30241 / 0 | 526 / 0 | 30767 / 0 | 19968 / 0 | 10273 / 0 |
| 2 / baseline / extraction | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / baseline / probe | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / baseline / other | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / build / answer | 15057 / 0 | 260 / 0 | 15317 / 0 | 6272 / 0 | 8785 / 0 |
| 2 / build / extraction | 13094 / 0 | 228 / 0 | 13322 / 0 | 12544 / 0 | 550 / 0 |
| 2 / build / probe | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / build / other | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

Baseline total: 0.02575216 CNY; build total: 0.02354064 CNY.
Signed descriptive difference: 0.00221152 CNY; percentage: 8.587707%.

Negative percentages mean the build sequence cost more. No positive-savings threshold is applied.

- No gold was read; supported status is not independent answer correctness.
- Signed cost difference is descriptive, not a quality-adjusted or general savings claim.
- Ingestion, local compute and storage cost are excluded and remain unmetered.
- Only this frozen sequence is totaled; project opening balances and other historical costs require separate ledger reconciliation.
- Missing usage fields remain missing; reported totals are not inferred.
- Latency starts at driver polling, not request dispatch or token generation.
