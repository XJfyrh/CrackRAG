# Independent same-page financial quality set

Status: **PREPARED_NOT_EXECUTED**. `no_api_calls: true`; model API calls during preparation: **0**. This is a frozen test input package, not a successful live evaluation report. The execution owner must bind the release commit/config, price snapshot, available budget and actual run IDs before reporting results.

The set contains two previously unused issuers, six supported financial facts, four refusal cases and two reuse cases. Its 16-query cost companion compares two four-query sequences for each issuer. This small set tests the release's declared same-page support; it does not cover scans/OCR, complex cross-page tables or broad financial question quality.

## Sources and manually checked facts

Both originals are issuer disclosures hosted by CNINFO, downloaded and verified on 2026-09-16. The original PDFs, complete extracted text, selected PDFs and rendered pages stay in a private directory outside the repository. The source URLs, byte counts and complete SHA-256 digests are in [sources.json](sources.json). These third-party reports are not distributed under this repository's MIT license.

| Issuer and official report | Original PDF page | Printed page | 2024 consolidated revenue, CNY yuan | 2024 consolidated cost of revenue, CNY yuan | 2024 total consolidated net profit, CNY yuan |
| --- | ---: | --- | ---: | ---: | ---: |
| [福耀玻璃 / 600660](https://static.cninfo.com.cn/finalpage/2025-03-19/1222834034.PDF) | 200 | 200 / 200 | 39,251,657,267 | 25,030,877,441 | 7,504,038,370 |
| [安井食品 / 603345](https://static.cninfo.com.cn/finalpage/2025-04-29/1223385636.PDF) | 106 | 105 | 15,126,651,674.36 | 11,602,494,309.32 | 1,513,618,588.14 |

Each selected positive page visibly contains the full issuer name, consolidated income-statement title, year column labels, currency/unit header and all three rows. Fuyao uses its original five-year performance summary. Anjoy uses the detailed consolidated statement; its footnote column is separate from the two numeric year columns. Values are taken from the 2024 column, not the 2023 comparative column. `净利润` means total consolidated net profit; attributable-to-parent profit is a different metric. Gold contains exact decimals and comparative-year checks.

All four selected original pages were rendered with Poppler at 140 DPI and inspected. The reproduction script copies original pages without adding headers, reformatting tables or supplying absent context. Selected-page decoded content streams, extracted text and page bounds were checked against the originals. The four selected-page renders were byte-identical to the inspected original-page renders.

The initial Wuliangye and Luzhou Laojiao candidates were retained privately as preparation-only records: their required rows and headers span different original pages. They were replaced before any model test, to fit the declared scope. Fuling was screened in text and also not selected. See [preparation-candidates.json](preparation-candidates.json). No candidate is labelled a model pass or failure.

## Twelve quality cases

The exact questions and API parameters are frozen in [model-inputs.json](model-inputs.json). [gold.json](gold.json) and [quality-plan.json](quality-plan.json) are evaluator-only inputs and must never be passed to the model, ingestion, retrieval or agent tools.

| Case group | Count | Required outcome |
| --- | ---: | --- |
| Each issuer's revenue, cost and total net profit | 6 | Correct supported claim, unit/year/entity/basis and original-page provenance. Abstention fails a positive case. |
| Fuyao original page 94 alone; ask total net profit | 1 | Explicitly abstain because the continuation lacks the table's year-column labels, unit header and consolidated title. A report-year page header cannot supply numeric column mapping. |
| Anjoy original page 107 alone; ask revenue | 1 | Explicitly abstain: the revenue row and table header are absent. |
| Fuyao adjusted revenue; Anjoy attributable-to-parent profit | 2 | Explicitly refuse the unsupported basis/concept without silently substituting a supported metric. |
| Exact repeated Fuyao revenue; paraphrased Anjoy revenue | 2 | Correct full-coverage reuse with published fact provenance and no incremental paid model calls. A failed seed/build is blocked, never a pass. |

Each positive and negative gets a fresh tenant and a normal upload. Only a reuse case shares its own successful revenue seed's context. Wait for seed-triggered background work and ledger settlement before reuse. Negative tenants must not inherit positive-page access or prior facts. The negative PDFs are genuine original continuation pages, not edited documents. Upload only the selected PDF for the requested scope; uploading the full original would invalidate the missing-evidence cases.

Grade the 6 positive, 4 negative and 2 reuse denominators separately. A crash, timeout or provider error is never a correct refusal. Do not count `INCONCLUSIVE` as a supported fact or hide abstained positives inside an aggregate pass rate.

## Sixteen-query cost measurement

[sequence-plan.json](sequence-plan.json) freezes `[revenue, cost, repeated revenue, paraphrased revenue]` for each issuer and arm. Both arms use strict `mode=m3`, `financial-supported-v1`, and `COLD_ALLOWED`. Baseline uses `build_facts=false`; build uses `build_facts=true`. Each arm has an independent fresh tenant and document upload. Quality tenants are separate. All arms use the same instance, project ledger and runtime/model/price configuration.

Record all attributable foreground, background, probe/routing, build, recovery and retry calls. Wait for all triggered work to settle before the next step. Report the complete four-query cumulative cost as well as step 3/4 reuse cost, source-answer correctness and latency. Missing usage or unsettled costs remain unknown/reserved; local compute is separately measured or unknown. Two small paired sequences cannot establish general cost savings or exact provider prefix-cache insertion, coverage or TTL.

## Reproduce privately

Use Python with `pypdf==6.8.0`; install Poppler only if rendering. Choose a new private directory outside this repository. The downloader accepts only allowlisted HTTPS report URLs, caps originals at 20 MiB, verifies exact size/SHA/page count and refuses differing existing artifacts. It reads only `sources.json`; it has no model/API-key capability.

```sh
python -m pip install pypdf==6.8.0
python scripts/release_prepare_quality.py --destination /path/outside/repo/private-quality --render
python scripts/release_prepare_quality.py --destination /path/outside/repo/private-quality --verify-existing
```

On Windows, pass an absolute private directory and optionally `--pdftoppm C:/path/to/pdftoppm.exe`. `--report` writes a private JSON provenance report with original/selected hashes and the selected-to-original page map. Select a new report filename if parameters change: reports are not overwritten.

Selected files are `selected/fuyao-positive.pdf`, `selected/fuyao-missing-header.pdf`, `selected/anjoy-positive.pdf` and `selected/anjoy-missing-revenue.pdf`. Every selected PDF has one physical page. Preserve its recorded mapping to original page 200, 94, 106 or 107 when scoring citations; Anjoy printed page numbers are one less than its original PDF physical page.

`preparation.json` freezes hashes of this package and the preparation script. It records preparation evidence only. Keep any subsequent live report separate and report failures honestly without editing this gold or switching sources after seeing model outcomes.
