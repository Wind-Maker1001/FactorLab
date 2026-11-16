# P7-M17 — Binance Demo real-fill acceptance

**NO LIVE MONEY.** Baseline: `7e49d41` from `origin/main`. M16 PR #184 was merged at `291370ce3b23d23111e316e17888e9628d7941e7` on 2026-11-16.

**DEMO_ENVIRONMENT_VERIFIED · BLOCKED_AUTHORIZATION · REAL_DEMO_FILL_NOT_VERIFIED**

## Actual Demo observation

`preflight.json` records a real, authenticated, GET-only observation ending **2026-11-16 13:18:11 UTC**. This is not the M16 HTTP fixture. The existing read-only transport made **12 GET requests**, all returning HTTP 200; **POST 0, DELETE 0**. Its endpoint receipt contains paths and response statuses, not signatures, API keys or Secrets.

Environment confirmation combines the explicit DEMO credential type, the persisted environment/host/credential-reference binding, successful signed Demo responses and equality of the signed-read credential account fingerprint with the existing binding. The account identifier is the existing credential-derived fingerprint, not an independently obtained exchange UID. Host spelling alone is not environment proof.

| Observation | Exchange evidence |
| --- | --- |
| Environment / base URL | DEMO / `https://demo-fapi.binance.com` |
| Product | NVDAUSDT, `TRADIFI_PERPETUAL`, USDT collateral and quote |
| Symbol capability | `TRADING`; LIMIT and MARKET advertised |
| Quantity | min 0.01; step 0.01 |
| Price | tick 0.01000 |
| Minimum opening notional | 5 USDT |
| Account mode / margin / leverage | ONE_WAY / CROSSED / 1 |
| Account `canTrade` | true |
| API-key trade permission | UNKNOWN; signed reads do not establish write permission |
| Positions / open orders | zero / zero |
| Wallet / available USDT | 4999.97114760 / 4999.97114760 |
| Unrealized PnL / initial margin | zero / zero |

The current market prices and hypothetical minimum sizes retained in the existing preflight output are sizing observations, **not strategy proposals or approved order prices**. No strategy was frozen, changed or dispatched using these observations. Symbol capability advertisement does not establish successful order execution.

Read-only local authorization inspection found **no active campaign**, no active expiry and no current order budget. No enable/setup/start operation, permission request or budget modification was performed. This stops the execution branch before any order submission.

## Account comparison and execution evidence

`local-comparison.json`, observed at **2026-11-16 13:19:02 UTC**, reads the existing runtime and controller SQLite databases using `mode=ro`; it does not initialize a new runtime or recover into the historical journals.

- Existing local wallet: **4999.97114760 USDT**.
- Exchange wallet from the preceding observation: **4999.97114760 USDT**.
- Exchange minus local wallet: **0.00000000 USDT**.
- Existing local positions: none; exchange positions: none.
- Seven historical local orders and seven stored confirmations remain historical identities, not M17 orders or trade receipts.

These are separate timestamped snapshots, not a new authoritative recovery round. Wallet agreement alone does not establish `DEMO_ACCOUNT_RECONCILED` for an M17 trade lifecycle. No unexplained balance difference was observed in this comparison; no local economic event was added.

| Required association | M17 actual result |
| --- | --- |
| Frozen strategy proposal / original order price | No new execution experiment authorized |
| clientOrderId / exchange orderId / ACK | No M17 order submitted |
| tradeId / quantity / price / commission / commission asset | No M17 trade history collected |
| Partial / cumulative fill reconciliation | Offline verified; real M17 evidence missing |
| Confirmed BUY inventory / reduceOnly SELL | Offline verified; real M17 evidence missing |
| Realized FIFO PnL / fees / funding | No M17 economic posting; missing actual receipts are not zero-valued cash flows |
| Restart while pending / after completion | Offline verified; real M17 lifecycle not exercised |
| Repeated GET recovery without duplicate booking or submission | Offline verified; real M17 lifecycle not exercised |

No order was under observation, so this run has **no order-level right-censored sample**, rather than an invented unfilled order or observation window. Prior M13/M15 proposals and frozen historical experiments are unchanged. The R12 financial protocol remains pending approval; engineering evidence does not activate economic qualification or production trading.

## Evidence conditions

Statuses are independent evidence claims, not an automatically advancing sequence. The read-only entrypoint emits only environment confirmation and explicit blocked/not-verified outcomes; the other conditions below govern a future actual execution assessment.

| Status | Necessary evidence | This run |
| --- | --- | --- |
| `DEMO_ENVIRONMENT_VERIFIED` | Explicit Demo credentials and persisted environment/host/reference binding, successful signed Demo reads, matching bound account fingerprint | Verified |
| `DEMO_ORDER_ACK_VERIFIED` | Authorized original strategy intent linked to actual successful exchange ACK, clientOrderId and orderId | Not established |
| `DEMO_TRADE_RECEIPT_VERIFIED` | Independent order GET and actual userTrades rows with matching symbol, side, orderId, unique tradeId, quantity, quote and actual commission/asset; totals equal authoritative cumulative order totals | Not established |
| `DEMO_ACCOUNT_RECONCILED` | Existing controller/adapter authoritative recovery accepts confirmed economic deltas and exchange balances/positions, with any difference explicitly retained and no unexplained discrepancy | Not established for M17 |
| `REAL_DEMO_FILL_VERIFIED` | Real-source order and trade receipts plus account reconciliation and pending/completed restart/repeated-GET evidence, including the authorized confirmed-inventory reduceOnly SELL lifecycle | Not established |
| `REAL_DEMO_FILL_NOT_VERIFIED` | At least one required actual execution/reconciliation/restart evidence item absent or conflicting | Applicable |
| `BLOCKED_AUTHORIZATION` | Missing valid Demo credential/binding, experiment-specific write authority, permission confirmation or unexpired resource/window coverage | Applicable: no active campaign or new experiment authority; key trade permission unknown |
| `BLOCKED_EXCHANGE_CAPABILITY` | Actual exchange evidence demonstrates the requested Demo product, order type or required receipt/account operation unsupported | Not asserted: advertised product capability is present |

ACK, a touched price, a FILLED label without trades, local historical confirmations and synthetic receipts cannot satisfy the real-trade conditions. Non-USDT commissions remain unsupported by the current projection and must block rather than receive an invented conversion. UNKNOWN after timeout must use GET-only resolution, never replacement POST. M14 full authoritative historical reads are retained; there is no terminal-order skip, cursor or second ledger.

## Offline verification

Python 3.14. The first baseline command completed with **81 passed, 1 failed in 37.30s** (`offline-tests.xml`). The failed Windows cross-process credential test inherited the environment's installed older source instead of this checkout. With explicit `PYTHONPATH=src`, that test and the six new admission tests completed with **7 passed in 1.83s** (`admission-tests.xml`). All **88 distinct selected tests** passed across these runs. Initial interpreter discovery also encountered an interpreter without pytest and an incorrect executable location before the installed test environment was selected; neither invocation ran tests.

The reused M16 tests cover partial fills, real-fee-vs-estimate accounting, FIFO, conservation, reduceOnly rejection, duplicates, interrupted transactions, lost ACK/GET timeout and pending/completed restart. Existing network/runtime tests cover environment/credential isolation and persistent authorization budgets. New tests exercise signed-read versus execution claims, unavailable identity, account mismatch, production credentials before network, missing binding and exclusive evidence-file creation.

From the checkout root, using Python 3.13/3.14 with the project test dependencies:

```bash
PYTHONPATH=src python -m pytest -q \
  tests/integrations/test_binance_demo_fill_closed_loop.py \
  tests/integrations/test_binance_demo_fill_faults.py \
  tests/integrations/test_binance_demo_network.py \
  tests/integrations/test_binance_demo_runtime.py \
  tests/integrations/test_binance_demo_acceptance.py
```

These tests intercept exchange HTTP and use synthetic credentials. They do not submit real Demo orders.

## Actual Demo procedure and missing evidence

The new read-only capture command consumes only the existing Windows Credential Manager binding and refuses to overwrite evidence:

```bash
PYTHONPATH=src python -m factorlab.integrations.binance_futures_trading.demo_acceptance \
  --output /path/to/new-preflight.json
```

It does not enable authorization, dispatch, cancel, restart historical execution state or infer API-key trade permission. A subsequent authorized run must use a fresh evidence destination; this snapshot is not reusable as fresh dispatch admission.

TODO: obtain the following actual evidence only within a separately valid, explicitly frozen and authorized Demo experiment:

1. Account-specific scope covering symbol, original strategy/price definition, quantity, total notional/risk, POST/DELETE/**total GET** counts, start/deadline and residual-inventory handling. Verify key write permission without unauthorized probe orders. The existing persistent campaign alone is not proof of all these experiment-specific bounds. Do not run enable-demo merely to satisfy this acceptance.
2. Reuse DemoGridBridge → DemoSession → TradingController → DemoExecutionGateway → DemoFuturesAdapter and their existing runtime guards. Review the persisted deployment before using `tools/demo_grid_acceptance.py`; its new-deployment factory derives prices from the initial quote and must not replace a frozen experiment. Submission requires the reviewed experiment's exact strategy proposal and original price, not the hypothetical preflight size.
3. Retain the sanitized actual ACK, exact order GET and order-specific `/fapi/v1/userTrades` rows. Link each unique trade to its order and strategy identity; compare cumulative quantities/quote and actual fee asset/value. Never count an ACK-only or FILLED-only response as the complete chain.
4. Recover using the existing authoritative account path. Record exchange/local wallet, available funds, positions, realized/unrealized PnL, commissions, and funding evidence if any; unexplained differences stop writes. Use only confirmed inventory for a legal original-price reduceOnly reversal; no unconfirmed FIFO realization.
5. Close/reopen against the same journals while the authorized order is pending and after completion. Retain each recovery's request counts and economic journal deltas, then repeat GET recovery to demonstrate unchanged economic postings and no additional POST. Preserve M14 full order/trade/account reconciliation.
6. Finish inside the approved request/order/notional/time bounds. If no natural fill occurs, retain the unfilled order and its right-censored interval; cancel/stop only as already authorized. Do not chase, amend repeatedly, raise budget or extend an expired window. A new experiment requires its own frozen identity and authorization.

The present authorization blocker is a terminal outcome for this run, not permission to manufacture a fill. Real Demo execution success, when later obtained, will still not authorize live-money production trading.
