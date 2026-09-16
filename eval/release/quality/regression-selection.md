# Regression selection record

On 2026-09-16 the source preparation process first examined Wuliangye and Luzhou Laojiao 2024 annual reports. Their headers and required rows crossed original pages, so they were retained privately as unsuitable for the declared same-page positive scope. Fuling was screened in text and not selected for the same reason. These decisions preceded model calls.

Fuyao's original page 200 and Anjoy's original page 106 were then visually checked as same-page candidates. The two originals and four unmodified page selections were hashed. Source URLs, original byte counts, SHA-256 hashes and exact physical/printed pages remain frozen in `sources.json`; numerical reference facts remain unchanged in `gold.json`.

During subsequent implementation review, these sources exposed three ordinary syntax gaps: a colon-terminated statement title with a multi-year summary header; a detailed statement's numbered total-net-profit row; and common Chinese scalar-query phrasing. Review also found that source-row labels accepted by answer verification must survive candidate concept mapping before the same semantic verification can authorize publication. These findings informed code changes and general synthetic tests. This is the point at which both issuers became **regression sources**.

No claim of independent holdout performance may use these two sources. No model calls were made by the preparation agent. The initial selection, frozen questions/gold, exact original PDFs and page images remain available for auditing; the later classification does not rewrite them. Any paid regression runs and their call accounting are separately owned and recorded by the release execution task.

Replacement holdout sources are to be selected only after review and code freeze, by manual inspection of original source pages against the documented capability boundary. They must not be passed through the validator with expected values during selection, and cannot be replaced because of a model failure while retaining the same acceptance claim.
