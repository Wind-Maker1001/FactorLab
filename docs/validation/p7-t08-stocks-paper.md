# P7-T08 Stocks rules and initial phase

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

Baseline: `b673f7f` (latest fetched `origin/main` at branch creation).

## Offline verification

Python 3.13.9; pytest 8.x; existing targeted integration fixtures and synthetic
Paper accounts. No production market observations are represented by these tests.

```sh
PYTHONPATH=src python -m pytest \
  tests/trading/test_stock_phases.py \
  tests/trading/test_rule_observations.py \
  tests/trading/test_realtime.py \
  tests/trading/test_simulation.py \
  tests/integrations/test_binance_equity.py \
  tests/integrations/test_binance_equity_trading.py \
  tests/integrations/test_binance_equity_trading_core.py -q
```

Result at execution: **231 passed in 34.26 seconds**. The earlier run had
228 passed and one failure: an existing test expected raw HTTP error retention.
That expectation was changed to assert no upstream error-body persistence.

A subsequently added recovery case was executed separately:
`tests/trading/test_stock_phases.py::test_unknown_initial_snapshot_can_be_replaced_by_valid_observation`
— **1 passed in 0.78 seconds**. Total distinct passing tests: **232**.
The corrected Calendar fixture also passed the existing synthetic dual-market
`run()` reconciliation assertions (fees: 0.2 USDT and 0.0375 USD).

Coverage includes missing Key, explicit permission/geographic denial, generic
HTTP and network errors, absent target, exact rule parsing and expiry; both
phase vocabularies, unknown/missing observations, duplicate/older/conflicting
transitions, stale-phase rejection, disconnect/restart reacquisition, bounded
large price frames retaining only NVDA evidence, and public-provenance checks.
Live mutation gates remain blocked after Paper admission. Shared Futures
regressions ran offline; no Futures online acceptance was repeated.

## Local online outcome

The process environment did not contain `BINANCE_EQUITY_API_KEY`. Only its
presence was checked; no credential value was inspected. No official NVDA
exchangeInfo request and no online Paper window were executed.

| Observation | Stocks NVDA outcome |
| --- | --- |
| Official rules | UNKNOWN / NOT_VERIFIED |
| Online market data | NOT_VERIFIED in this task |
| Official current phase | UNKNOWN / NOT_VERIFIED |
| Online Paper order | NOT_VERIFIED; none submitted |
| Online simulated fill | NOT_VERIFIED; none booked |
| Online fee/slippage | NOT_VERIFIED; no online execution |
| Online account reconciliation | NOT_VERIFIED; no online account created |

These outcomes do not replace the separately recorded observations from earlier
tasks. A later authorized run needs a locally supplied read-only Key, one NVDA
rule fetch, then a Stocks-only official WebSocket Paper window of at most
60 seconds. Missing rules or phase, no quote-driven order, or no simulated fill
must retain their individual UNKNOWN/NOT_VERIFIED/NO_TRADE outcomes rather than
produce a combined PASS.
