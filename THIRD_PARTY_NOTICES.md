# Third-party components and source material

The repository's MIT license applies to original CrackRAG code, documentation,
and the explicitly marked synthetic demonstration PDFs. It does not relicense
dependencies, models, issuer disclosures, or other third-party material.

## PDF parsing

The runtime uses **PyMuPDF 1.26.7**, which incorporates MuPDF. PyMuPDF/MuPDF are
available under AGPL or commercial licensing. This release retains the open
source dependency and its license; it does not claim that a combined runtime
is covered exclusively by MIT. Review the corresponding terms before modifying,
redistributing, or deploying a combined application.

- [Official licensing explanation](https://pymupdf.readthedocs.io/en/latest/about.html#license-and-copyright)
- [Versioned source](https://github.com/pymupdf/PyMuPDF/tree/1.26.7)
- [License text](https://github.com/pymupdf/PyMuPDF/blob/1.26.7/COPYING)
- [MuPDF source](https://github.com/ArtifexSoftware/mupdf)

The first release distributes original source and local build instructions.
The dependency lock and Docker build instructions identify the dependencies used. Dependency
licenses and applicable corresponding-source obligations remain in force.

## Embedding assets and other dependencies

BGE-M3 is downloaded separately using the fixed revision and per-file SHA-256
in `config/m1-embedding.json`. Its upstream model card supplies the model's MIT
license and attribution: [BAAI/bge-m3](https://huggingface.co/BAAI/bge-m3).
Model weights are not part of this repository or the source release.

Python dependencies are pinned in `ai-runtime/requirements-m1.lock`; Go modules
in `api/go.mod` and `api/go.sum`; frontend dependencies in `web/package-lock.json`.
Each dependency retains its original license. Generated protobuf code retains
its generator notices. Container base images and system packages retain their
respective licenses; image digests identify the build inputs.

## Documents and evaluation evidence

`web/public/samples/financial.pdf` and `ambiguous.pdf` are original, visibly
synthetic PDFs released under MIT. They are not actual issuer disclosures and
must not be presented as real financial data.

The static replay demo (`web/src/demo/`, published at `/demo/`) additionally ships
the **single cited page** of one publicly disclosed annual report as
`web/public/samples/demo-annual-report.pdf`. It exists because the demo shows the
highlighted evidence rectangle that the product drew on the original page, which
requires the page itself to render offline. It remains the issuer's material: it
is not relicensed by this repository's MIT grant, it must not be presented as
CrackRAG output, and it is used only to display a citation region that the
issuer's own public filing already contains. Remove that file and the demo build
falls back to synthetic samples only.

Real annual reports are obtained by the operator from official source links
and verified against the evaluation source manifest. Apart from the single
replay page above, this repository does not redistribute their full PDFs, page
screenshots, or original extracted bodies. Source links, file hashes, evaluation
methodology, independently recorded numeric checks, and aggregated results do
not relicense the original reports.

PDF.js (`pdfjs-dist`, locked in `web/package-lock.json`) renders authorized source
pages locally in the browser. It is licensed under Apache-2.0. Its original
LICENSE and the accompanying CMap/font/WASM notices are copied with the browser
assets at build time; see the [upstream license](https://github.com/mozilla/pdf.js/blob/master/LICENSE).

Database exports, browser traces, model request payloads, invoices, credentials,
and third-party research papers are excluded from the public source distribution.
Public evaluation summaries report methodology, results, and limitations.

## FanOutQA scoring adapter

`research/adc/evaluation.py` adapts the string algorithm, normalization, answer rendering,
and judge rubric; `research/adc/mediawiki.py` adapts the Markdown converter and the dated
revision/parse request semantics from [FanOutQA](https://github.com/zhudotexe/fanoutqa/tree/989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33),
commit `989f4c40d9deea1ecb0897d7a17a9c0fe20d5c33`. Copyright (c) 2024 Andrew Zhu, MIT.
The complete MIT notice is retained in both modules; source SHA-256 values are recorded there.
No FanOutQA dataset rows are redistributed by the new harness; dataset licensing remains
CC-BY-SA-4.0. The optional spaCy English model is downloaded explicitly from its official
release using the URL and SHA-256 in `research/adc/requirements-scoring.lock`, never bundled
or downloaded by the evaluator at runtime.
