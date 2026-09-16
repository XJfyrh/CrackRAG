# Current classification: regression sources

The Fuyao/Anjoy package in this directory was frozen before model calls, then used on 2026-09-16 to identify missing ordinary same-page grammar and request phrasing. It is therefore **a regression set, not an independent holdout or independent release acceptance result**.

The original frozen files and `preparation.json` remain unchanged as a preparation record. Their earlier “independent” description is historical and superseded by this classification. A later live result on these sources may establish regression behavior only. `no_api_calls: true` describes preparation, not any later execution by the release owner.

A new `holdout-v2` must be selected only after the relevant code is frozen. Its source pages may be manually checked against the declared scope, but must not be used for validator tuning before acceptance execution. Any source subsequently used for debugging must likewise be reclassified.
