# Software behavior equivalence

37 selected core-math module ASTs match immutable baseline Git blobs after
normalizing import package names only; see evidence/core-math-ast-comparison.json.
The inherited research math is retained. Renaming physical imports, installing
resources and introducing a new software identity are explicit migration
changes; they do not authorize historical financial evidence.

| Domain | Evidence and acceptance boundary |
|---|---|
|14 factors, cross-sectional processing, forward labels, DatasetBuilder|Shared-input factors and dataset are exactly equal, including dates, indexes, labels, dtypes and missing masks; frozen Golden/reference retained|
|FactorScore, Ridge, LightGBM, XGBoost|30-artifact comparisons pass on 3.14/3.13 at rtol=atol=1e-12; tree predictions exact; rejected XGB3.1.1 round retained|
|Walk-Forward and leakage prevention|Retained walk_forward, validation, inference and model robustness cases test expanding splits, purge/embargo, train-only preprocessing, maturity and prediction uniqueness|
|IC/RankIC and inference|Retained metric, comparison, statistical audit and robustness cases cover paired daily RankIC, HAC, moving-block bootstrap, Holm, missing days, constants, extremes and insufficient samples|
|Fast and Execution backtests|Shared event/NAV/trade/rejection tables match within declared tolerance; execution NAV and rejections exact; retained tests cover next-open timing, T+1, lots, suspensions, limits, cash, costs and equity|
|Factor discovery|Retained AST/provenance/refusal cases and independent installed synthetic CLI:39 successful expressions and1 exact duplicate in40 records; no formal or real-data qualification|
|Market structure/minutes|32 contracts remain,14 implemented synthetic features and18 CONTRACT_ONLY; retained temporal, taxonomy, source policy and5/15/30-minute tests; installed synthetic CLI completes with REAL_RESEARCH_BLOCKED|
|Forward|Retained freeze, activation, signals, temporal, full-prediction and evaluation contracts; physical executing-source binding added; formal path always additionally refuses pending independent review|
|Data source security|227 targeted CSMAR tests passed after Windows timestamp repair; full retained admission/provider/status/PIT/path/TOCTOU tests stay in CI|

The independent frozen factor reference is byte-identical to the baseline,
SHA256 `160c9869f962b28434ff7124418463e7e2530bad3f006d3879660ce1cfdab2e9`.
A test-only adapter verifies those bytes and directly compiles the same bytes.
Only calendar/errors imports map to their byte-equivalent renamed modules;
other qbot imports fail. There is no production Qbot compatibility package,
no loader cache dependency and no regenerated Golden. The reference math and
all original fixture/checksum bytes remain unchanged.

Final independent CI results are reported in INDEPENDENT_CI_REPORT.md. Both
final software suites each passed3915 cases under Windows3.14/3.13, with0
fail/error/skipped and8 deselections. The full suite remains an actual FAIL:
3980 pass,50 fail,136 error,2 network deselections; its historical/control
precondition failures are explicitly reviewed rather than counted as refusals.
The targeted426-case compatibility,227-case CSMAR and337-case reference/identity runs overlap
the full suite and must not be added to its unique-case count. Earlier diagnostic
full-r2 had3915 pass,75 fail,169 errors; it remains FAIL. Ordinary portability
failures were repaired, while unavailable historical controls remain explicit.

Expected metadata changes include package name, Python version, source/resource
hashes, project vs software Git identity and migration protocol. Old serialized
schema/domain strings retained for compatibility are not active Qbot imports.
No statistic, ranking rule, split definition, fee or fill model was tuned to
hide numerical drift. Daily bar execution remains an approximation to actual
liquidity and cannot certify tradability.

NO LIVE MONEY / PIT_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.
