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

Latest result: **234 passed in 34.30 seconds**, including the captured target-
omission pattern and explicit incomplete-rule rejection. An earlier failing
assertion expected raw HTTP error retention; it now asserts that upstream error
bodies are not persisted.
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

The first run's restart cleared runtime receipts before sufficient diagnostic
evidence had been retained. A separately authorized second acquisition retained
only the NVDA rule row and bounded target-only phase evidence before restart.

## Second online acquisition and diagnosis

Run identifier: `p7-t08-online-20261111T201632289341Z`. UTC window:
2026-11-11T20:16:32.304566Z–20:17:23.804889Z; elapsed **51.500 seconds**.
Exactly one additional official rule request, without retry, and one Stocks
WebSocket connection were used. No third acquisition was performed.

The official rule row omitted `minQty`. Its `stepSize` was `0.000000001`, but
quantity increment does not establish minimum allowed quantity. Parsing now
reports `INCOMPLETE_SYMBOL_RULES:minQty`; rule provenance is VERIFIED while
executable validity remains UNKNOWN.

The connection received 399 frames including ACK: 381 quotes and 17 price
frames. Five price frames contained NVDA, all with `mp=O` (REGULAR); twelve
omitted NVDA. No Calendar transition occurred. The original normalization
incorrectly turned omissions into UNKNOWN observations and conflicts. The fix
ignores omitted-target frames without revoking or renewing existing evidence;
explicit unknown NVDA phases and ambiguous duplicate targets still revoke.

There was one deterministic Paper acceptance signal, zero orders and zero
fills, with `UNKNOWN_RULES` rejection and NO_TRADE. Cash remained 1000 USD,
reserved cash zero, and positions/open orders empty. PaperStore matched runtime;
economic state matched after restart, excluding only the query timestamp.
Restart cleared rules, phase and process-local authorization.

## Offline replay of retained evidence

Replaying the second capture through the corrected normalization made zero
network requests: **17 recorded frames, 5 target observations, 12 ignored
omissions, 0 conflicts**. Final phase was REGULAR with UNVERIFIED provenance,
and no public authorization was granted. The retained rule row reproduced
`INCOMPLETE_SYMBOL_RULES:minQty`. This is offline regression evidence, not a
post-fix online acceptance result.

TODO: obtain authoritative minimum-quantity semantics for the omitted `minQty`
before enabling complete Stocks rule certification. Do not substitute zero or
`stepSize`. Online executable rules and simulated fills remain
UNKNOWN/NOT_VERIFIED; there is no combined online PASS.

First run identifier: `p7-t08-online-20261111T200944979321Z`.
Futures online acquisition and the full repository matrix were not repeated.
