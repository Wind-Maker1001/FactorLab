# Cross-version numerical validation

Fixed public synthetic generator, seed42, ten generated securities, shared
2019-09-02 through2020-12-31 input Parquet bytes. Research dates are2020 only;
pre-start observations provide factor warmup. No supplier or live gateway is
called. Baseline uses the independently installed qbot.research stage-one wheel
on3.9.25; target uses factorlab under3.14.0 and3.13.9.

30 artifacts per comparison: factor table, DatasetBuilder output, and seven
tables per model (predictions, daily statistics, Fast NAV/trades, Execution
NAV/trades/rejections). Tolerance is rtol=atol=1e-12; dtypes and table structure
are checked. Factor and Dataset values, dates, labels and indexes are exact.

Both final3.14 and3.13 comparisons PASS all30 artifacts. LightGBM and XGBoost
predictions match exactly; FactorScore/Ridge have small floating point changes
within the declared tolerance. Execution NAVs and rejection tables are exact.
The original Golden and independent frozen factor reference were not regenerated.

The rejected XGBoost3.1.1 round is FAIL, with aligned score differences up to
0.0011563635198399425 and material changes in ranking and both backtests. It is
not counted as a successful migration run. Keeping XGBoost2.1.4 on the new Python
restored exact predictions; no altered training options were needed.

Each model produced440 Test predictions. Baseline/final trade counts are:

| Model | Fast trades | Execution trades |
|---|---:|---:|
| FactorScore |36|36|
| Ridge |34|31|
| LightGBM |30|30|
| XGBoost |31|31|

Observed probe durations were20.668s on3.9 and16.653s on3.14. Other validation
processes ran concurrently, so this is NOT a controlled speed comparison.
Performance improvement is NOT_ESTABLISHED. Full inference edge cases and
backtest rejection semantics additionally rely on the retained test suites.

Machine-readable reports: evidence/numerical-comparison-r1.json (rejected),
evidence/numerical-comparison-r2.json (3.14 PASS), and
evidence/numerical-comparison313-r1.json (3.13 PASS).
