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

Executed one Stocks-only observation using a locally supplied read-only Key.
No credential was logged, persisted or committed. Exactly one official NVDA
exchangeInfo request was made, without retry. Recorded UTC times:
2026-11-11T20:09:45.005161Z–20:10:36.693598Z; total elapsed 51.688 seconds.

| Observation | Stocks NVDA outcome |
| --- | --- |
| Official rule acquisition | Response received through official MARKET_DATA GET; provenance VERIFIED |
| Executable rules | UNKNOWN; required-rule parsing did not produce VALID rules |
| Online market data | Observed: 456 quotes and 16 phase snapshots, all 472 normalized events accepted |
| Connection / subscription | 1 handshake, subscription ACK, 473 frames including ACK; no reconnect or connection failure |
| Official current phase | UNKNOWN at final observation; phase evidence conflict; 16 price snapshots, no Calendar transition observed |
| Strategy signal | 1 deterministic Paper acceptance signal, not a predictive claim |
| Online Paper order | 0; rejected with UNKNOWN_RULES |
| Online simulated fill | 0 / NOT_VERIFIED; NO_TRADE |
| Synthetic fee / slippage cost | 0 USD / 0 USD |
| Final synthetic account | Cash 1000 USD; reserved 0; no positions or open orders |
| Local persistence / restart reconciliation | Runtime matched PaperStore; economic account matched after restart |
| Restart authorization | Rules, phase and process-local authorizations cleared |

The original whole-account equality diagnostic was false because `observed_at`
is the time of each account query. A separate local-only comparison excluded
that query timestamp and confirmed identical economic state. It made no network
requests. This verifies persistence of an unchanged account, not execution or
exchange-account reconciliation.

The final snapshot was received at 20:10:33.438625Z and carried price time
1794427829027; transition-detection and actual effective times were unknown.
The observation was not authorized because its conflict flag was set.

TODO: identify the actual rule-field rejection and first conflicting phase
inputs before claiming the two online blockers resolved. The restart exercise
cleared runtime rule/phase receipts; the original result retained summary
validity but not the target rule row or per-snapshot phase identities needed
for that diagnosis. A further separately authorized acquisition must retain
only NVDA rule evidence and bounded NVDA phase observations before restart.
No second official rule request has been issued. Online rules, phase and
simulated execution remain UNKNOWN/NOT_VERIFIED rather than a combined PASS.

Local evidence run identifier: `p7-t08-online-20261111T200944979321Z`.
Futures online acquisition and the full repository matrix were not repeated.
