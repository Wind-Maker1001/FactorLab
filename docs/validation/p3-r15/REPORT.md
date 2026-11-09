# P3-R15: research execution costs, capacity and exposure diagnostics

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

## Protocol and interface

Published baseline: `d2f52c05ddaf86f493ae38c792aaf961ff652bb0`. Attribution dependency #40 is open, not released. The standalone extension accepts its `attribute_execution` callable; no attribution engine is copied. Integration uses source from commit `6a02db0` explicitly.

The experiment is SYNTHETIC / SOFTWARE_ONLY. Three securities, deterministic daily prices and fixed rotating model scores drive the existing score optimizer and holdings-aware feedback adapter. No fitting or parameter selection occurs. Investment .8, cap .5, trailing risk five sessions and rebalance every three prediction observations are fixed. A 3×2 grid varies CNY 10,000/100,000/1,000,000 and historical-volume participation .02/.10, with base and zero-additional-cost references at each capital. Additional slippage 2bps and impact coefficient 20bps are research assumptions, not observed execution quotes. Outcomes are descriptive, not real strategy capacity certification.

`ResearchExecutionScenario` is optional on `AShareExecutionBacktester` and `ResearchPipelineConfig`. Original fee defaults remain unchanged. New fixed slippage and market impact are subcomponents of the existing additive `slippage`; transaction cost includes them exactly once. Impact cost is `notional * coefficient_bps / 10000 * sqrt(order_quantity / historical_average)`, in consistently shares/day or CNY/day units. Execution price stays the existing next-open daily approximation.

Liquidity inputs are explicit prior-history averages with effective date, known-at, source and SYNTHETIC/HISTORICAL provenance. Selection is limited to the signal close at 15:00; an execution-day daily total is not an opening-liquidity observation. Limit is participation × average, consumed by aggregate actual buy and sell fills per security/session, not requested orders. Partial policy rounds down legal quantities; reject policy rejects the capacity-exceeding remainder in full. Existing status, price-limit, T+1, sale-lock, cash and lot rules remain in force. Missing required liquidity rejects execution as `liquidity_NOT_COMPUTABLE`, never an unconstrained fill. No deferred remainder is retried without another target. If even a sale's fee cannot be funded from cash plus proceeds it is refused.

Order rates use unique signal-date/execution-date/security/side identities. Partial-refusal records are not additional submitted orders. Share fill rate is filled shares/requested shares; rejection rate is fully rejected orders/all orders. Target desired holdings are not submitted quantities. Unknown-quantity target failures stay separate from quantity-based order rates.

`execution_diagnostics` extends the injected R14 ReturnAttribution. The original five cost columns still reconcile; new subcomponents disclose fixed research slippage and impact. Gross P&L is explicit fees added back on the same observed fills, not a zero-cost rerun. Target turnover comes only from a decision-time actual-holdings snapshot. Different-path daily return comparisons are marked HYPOTHETICAL_REEXECUTION_DIFFERENCE_NOT_ATTRIBUTION; they do not identify an additive execution contribution.

`diagnose_exposures` reports NAV-weighted security HHI/max, industry weight/HHI and caller-supplied market-cap/momentum/volatility weighted values. Inputs carry source, unit, effective and knowledge time; eligible observations precede the session's 15:00 close. Every nonzero holding must be covered, in common units and provenance type. Missing, invalid or ambiguous selected inputs yield NOT_COMPUTABLE. No future backfill, inferred historical industries or commercial Barra model is provided. Synthetic style units are CNY market capitalization, dimensionless momentum return and daily-return volatility.

## Implementation and verification state

Execution core, optional pipeline binding, injected attribution adapter and historical diagnostics are implemented. Initial execution regression: 62 passed, 2 failed in 3.71s. Failures were empty-fill object dtype in order accounting and a test passing a DataFrame to the session's mapping API; both corrected. The next targeted run passed 97 tests in 6.21s. Separate R14 integration plus existing optimizer, full research-pipeline and execution regression passed 36 tests in 41.99s. The empty-frame pandas warning from those runs was removed by explicit quantity dtype conversion.

The initial 12-scenario grid completed, reconciling every account with the injected R14 function and ResearchEvaluation. At CNY100,000 and 2% participation, share fill rate was 11.19%, cumulative gross turnover .5461 and net return .3639%, compared with the no-added-constraint reference net return 1.5435%. At CNY1,000,000 and 2%, fill rate fell to .8690% and net return .03738%. These are consequences of the disclosed synthetic liquidity assumptions, not real capacity estimates. All constrained orders in this grid had at least a partial fill; fully rejected-order rates were zero, while partial-order rates were reported separately. Deterministic tests exercise full rejection and missing-liquidity rejection.

The subsequent run passed 103 tests and exposed one pre-existing integration issue in the new all-rejected pipeline case: empty execution trade tables used object dtype, which ResearchEvaluation correctly rejects for monetary fields. Empty trade schema construction is now centralized in the existing execution module and reused by batch, incremental and single-security adapters. Nonempty receipt types and economic results are unchanged.

**Final targeted verification: 153 passed, zero skipped, no warnings, 20.75 seconds.** This covers optional configuration and tracker binding, empty-book evaluation, deterministic partial/rejected orders, cash/holdings/fees, capacity aggregation, nonlinear affordability, expensive-sale refusal, batch/session parity, fixed-fill monotonicity, future signal/risk/liquidity perturbations, historical exposure boundaries and existing single-security execution. The separate 36-test compatibility run additionally covered the existing four-model research pipeline and portfolio optimizer. Test receipts record each run separately rather than adding overlapping counts.

**P3R15_ENGINEERING_ACCEPTED.** The final 12-account experiment completed in 7.44 seconds; every account passed R14's same-path cash/fees/shares/equity/NAV checks and ResearchEvaluation. At each of the three capital levels the default and zero-additional-cost results match on NAV, fills, holdings, rejections and weight comparisons. The fixed-fill fee stress increases cost without changing fills; changed-capacity paths are checked for internal consistency, not mechanically monotonic returns.

Final compact evidence is in `evidence/`: all scenario summaries and disclosed liquidity assumptions (`results.json`, `summary.csv`), original scores, paired hypothetical daily differences, aggregated exposure ranges/means, fixed-fill repricing, and the CNY100,000/2% participation sample's decisions, target/actual weights, orders, trades, refusals and same-path attribution. `receipt.json` records executed source identities, dependency identity, test results and artifact bindings. Executed provenance is the published baseline plus the uncommitted implementation identified by these source hashes, not a claim that a later commit was the executed clean HEAD.

Resources: CPU-only synthetic experiment, three securities, 32 sessions, 75 prediction rows, 12 independent existing-engine accounts and zero model fits; generated experiment outputs total 810,064 bytes, with approximately 126 KB of compact retained evidence. No new optimizer framework, live connection or large model artifact is introduced. Full reproducible local experiment tables and the explicit dependency source are retained locally; redundant development outputs are removed after evidence retention.

Research boundaries: historical averages are caller-provided liquidity proxies with no order-book, queue, intraday seasonality or real fill calibration. Square-root impact and partial allocation are disclosed research assumptions. The experiment does not establish real strategy capital capacity. Style exposures use supplied units without cross-sectional normalization or factor-return/covariance estimation. Real historical availability remains subject to the unchanged PIT/HFQ/formal gates.

## Reproduction

Run in Git Bash at the repository root, Python 3.13 with existing project dependencies:

```sh
export PYTHONPATH=src
PY=/d/Environments/factorlab-py313/Scripts/python.exe
mkdir -p local-r15
# Explicitly obtain the unreleased dependency from its fixed commit.
git show 6a02db0:src/factorlab/evaluation/return_attribution.py > local-r15/r14_return_attribution.py
export FACTORLAB_R14_ATTRIBUTION_SOURCE="$PWD/local-r15/r14_return_attribution.py"
$PY -m pytest -q tests/research/test_execution_capacity.py tests/research/test_risk_exposures.py tests/research/test_execution_risk_integration.py tests/research/test_capacity_pipeline.py tests/research/test_portfolio_execution.py tests/research/test_execution_feedback.py tests/research/test_pipeline_feedback.py tests/research/test_research_evaluation.py tests/research/test_dual_ma.py
$PY -m examples.research.execution_capacity_synthetic --output local-r15/new-run --attribution-source "$FACTORLAB_R14_ATTRIBUTION_SOURCE"
```

On a fresh clone, fetch the public dependency reference first if the object is absent: `git fetch origin codex/p3-r14-return-attribution`. Integration tests require the explicit environment variable; standalone execution/risk tests do not. `--output` must name a new directory. `--capitals` and `--participation` configure a small sensitivity grid without modifying predictions or historical inputs.

The output includes the original predictions, target weights, decision-time actual weights and target turnover, actual fills/rejections, holdings, target-vs-actual execution weights, same-path daily attribution, order outcomes and historical exposures. The compact checked-in evidence retains summaries, risk diagnostics and paired differences rather than duplicate full per-scenario tables. Pure-cash dates are retained when the exposure calendar is supplied: concentration is zero, missing style evidence remains unavailable.
