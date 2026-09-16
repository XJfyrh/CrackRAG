# Short-sequence model cost measurement

Known, settled model-cost estimates in CNY. Supplier billing is not confirmed; answer correctness was not independently evaluated by this script.

A separate [independent review](../eval/release/quality/holdout-v2/FIRST-RUN-QUALITY.md) subsequently verified all 16 sequence answers against the original pages and published facts. The cost comparison below applies to those 16 correct answers. The separate 12-case quality gate failed one supported positive (5/6); this is a preserved pre-fix measurement, not the final release acceptance or evidence of general savings.

Release manifest SHA-256: `d183620ebaebeba6e1cb7994faed9dc78c047017c47fad598b42f754aa4e00d6`.
Frozen model inputs SHA-256: `cb3b1c50fb55d516c1a1a29e052acae2c1742eb5a6c14a773cdba6c6ed9bfaaa`.
Summarizer SHA-256: `bc77d50b0f0027eb5a28078e9806faef2606a9c904cc6180b9ad0f013dc9e387`.

| Case | State | Answer | Coverage | Calls | Estimated CNY | Zero-call validated reuse | Background terminals |
| --- | --- | --- | --- | ---: | ---: | --- | --- |
| s-longsheng-baseline-1 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00875500 | False | none |
| s-longsheng-baseline-2 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00197424 | False | none |
| s-longsheng-baseline-3 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00085228 | False | none |
| s-longsheng-baseline-4 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00198524 | False | none |
| s-longsheng-build-1 | COMPLETED | SUPPORTED | MISSING | 2 | 0.01635800 | False | COMMITTED: 1 |
| s-longsheng-build-2 | COMPLETED | SUPPORTED | MISSING | 2 | 0.00269604 | False | COMMITTED: 1 |
| s-longsheng-build-3 | COMPLETED | SUPPORTED | FULL | 0 | 0 | True | none |
| s-longsheng-build-4 | COMPLETED | SUPPORTED | FULL | 0 | 0 | True | none |
| s-hengli-baseline-1 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00799100 | False | none |
| s-hengli-baseline-2 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00187744 | False | none |
| s-hengli-baseline-3 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00084092 | False | none |
| s-hengli-baseline-4 | COMPLETED | SUPPORTED | MISSING | 1 | 0.00184844 | False | none |
| s-hengli-build-1 | COMPLETED | SUPPORTED | MISSING | 2 | 0.00882444 | False | COMMITTED: 1 |
| s-hengli-build-2 | COMPLETED | SUPPORTED | MISSING | 2 | 0.00259544 | False | COMMITTED: 1 |
| s-hengli-build-3 | COMPLETED | SUPPORTED | FULL | 0 | 0 | True | none |
| s-hengli-build-4 | COMPLETED | SUPPORTED | FULL | 0 | 0 | True | none |

| Pair / arm | Stage | Calls | Estimated CNY | Failed HTTP calls |
| --- | --- | ---: | ---: | ---: |
| 1 / baseline | answer | 4 | 0.01356676 | 0 |
| 1 / baseline | extraction | 0 | 0 | 0 |
| 1 / baseline | probe | 0 | 0 | 0 |
| 1 / baseline | other | 0 | 0 | 0 |
| 1 / build | answer | 2 | 0.01059980 | 0 |
| 1 / build | extraction | 2 | 0.00845424 | 0 |
| 1 / build | probe | 0 | 0 | 0 |
| 1 / build | other | 0 | 0 | 0 |
| 2 / baseline | answer | 4 | 0.01255780 | 0 |
| 2 / baseline | extraction | 0 | 0 | 0 |
| 2 / baseline | probe | 0 | 0 | 0 |
| 2 / baseline | other | 0 | 0 | 0 |
| 2 / build | answer | 2 | 0.00971900 | 0 |
| 2 / build | extraction | 2 | 0.00170088 | 0 |
| 2 / build | probe | 0 | 0 | 0 |
| 2 / build | other | 0 | 0 | 0 |

Usage cells show the reported total / number of attempts missing that field. Missing counts are never inferred as zero.

| Pair / arm / stage | Input | Output | Total | Cache hit | Cache miss |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 / baseline / answer | 32849 / 0 | 542 / 0 | 33391 / 0 | 21888 / 0 | 10961 / 0 |
| 1 / baseline / extraction | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 1 / baseline / probe | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 1 / baseline / other | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 1 / build / answer | 16387 / 0 | 278 / 0 | 16665 / 0 | 7040 / 0 | 9347 / 0 |
| 1 / build / extraction | 14220 / 0 | 252 / 0 | 14472 / 0 | 6912 / 0 | 7308 / 0 |
| 1 / build / probe | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 1 / build / other | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / baseline / answer | 29833 / 0 | 542 / 0 | 30375 / 0 | 19840 / 0 | 9993 / 0 |
| 2 / baseline / extraction | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / baseline / probe | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / baseline / other | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / build / answer | 14879 / 0 | 278 / 0 | 15157 / 0 | 6400 / 0 | 8479 / 0 |
| 2 / build / extraction | 13010 / 0 | 246 / 0 | 13256 / 0 | 12544 / 0 | 466 / 0 |
| 2 / build / probe | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| 2 / build / other | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |

Baseline total: 0.02612456 CNY; build total: 0.03047392 CNY.
Signed descriptive difference: -0.00434936 CNY; percentage: -16.648548%.

Negative percentages mean the build sequence cost more. No positive-savings threshold is applied.

- No gold was read; supported status is not independent answer correctness.
- Signed cost difference is descriptive, not a quality-adjusted or general savings claim.
- Ingestion, local compute and storage cost are excluded and remain unmetered.
- Only this frozen sequence is totaled; project opening balances and other historical costs require separate ledger reconciliation.
- Missing usage fields remain missing; reported totals are not inferred.
- Latency starts at driver polling, not request dispatch or token generation.
