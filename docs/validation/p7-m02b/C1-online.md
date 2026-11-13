# P7-M02B-C1 official Demo online verification

**P7_M02B_C1_BLOCKED** · **P7_M02B_C1_REAPPROVAL_REQUIRED**

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

## Observation

The official Demo GET preflight completed at **2026-11-13 09:52:18.180201 UTC**
against `https://demo-fapi.binance.com`, on repository baseline
`5c80fbe116af862d2b8bf61b8918c3da229e470e`.
The complete credential-derived account ID matched the original local C1
approval exactly. No suffix-only comparison was used. The two approved local
journal files were absent before the preflight.

The original [approved candidate](C1.md) was still before its absolute expiry,
2026-11-13 11:00:00 UTC, but fresh market evidence invalidated its price and
budget admission. No `DemoNetworkAuthorization` was created.

| Input | Official Demo observation |
| --- | --- |
| Clock | Fresh `/fapi/v1/time` midpoint offset **−1,274 ms**; no historical offset adjustment |
| Mark | **169.54834487 USDT**, exchange timestamp **09:52:15.592 UTC** |
| Bid / ask | **169.10 / 169.81 USDT**, exchange timestamp **09:52:14.419 UTC** |
| Account | `canTrade=true`, `ONE_WAY`, NVDAUSDT **1×**, `CROSSED` |
| Exposure | **0** nonzero positions; account and position-risk observations agreed; **0** open orders |
| Funds | Wallet and available balance **5,000 USDT** each; unrealized P&L and initial margin **0 USDT** |
| Quantitative rules | Valid observation; ACCOUNT and NVDAUSDT `NOT_REPORTED`, no reported applicable lock |
| Contract | `TRADING`, `TRADIFI_PERPETUAL`, USDT quote and margin |
| LIMIT filters | Minimum quantity **0.01**, step **0.01 NVDA**, minimum notional **5 USDT**, tick **0.01 USDT** |
| Price band | **164.4618945239–174.6347952161 USDT**, 0.97–1.03 × observed mark |
| Exact approved intent | `c1-6bb207a20bf64c68-01`, BUY LIMIT GTC, **0.04 NVDA @ 165.60 USDT**, BOTH, reduceOnly=false |

## Admission failure

The approved limit remains below the observed bid and within the mark band.
Its exchange notional remains **6.624 USDT**, and 0.04 NVDA remains the minimum
legal quantity at that limit. However:

- Ask **169.81 USDT** exceeds the explicit **167.00 USDT** ceiling.
- Full-notional occupancy is `0.04 × max(165.60, 169.81)` = **6.7924 USDT**,
  exceeding the **6.68 USDT** cap.
- The controller's 0.10% fee-inclusive reservation would be **6.7991924 USDT**,
  exceeding the **6.69 USDT** margin budget.
- `review_opening_plan()` returned `PLAN_NOTIONAL_LIMIT` and `PLAN_MARGIN_LIMIT`.

This is a pre-execution risk block, not an exchange rejection or a permissions
failure. Key-level TRADE permission remains unknown. The exact-ID exchange
lookup and durable-journal admission were not reached after the market block;
no claim is made that this preflight established exchange order-ID absence.

## Request receipt and outcome

Actual traffic was **12 GET**, **0 POST**, **0 DELETE**, **0 other methods**.
All twelve GET requests returned HTTP 200:

`time`, `exchangeInfo`, `leverageBracket`, `account` V3, `positionRisk` V3,
`positionSide/dual`, `openOrders`, `symbolConfig`, `account` V2,
`apiTradingStatus`, `premiumIndex`, and `ticker/bookTicker`.

The GET-only transport had no order-write capability. There was no submission,
cancellation, fill, commission, or newly created position from this attempt.
The observed account remained flat with no open orders. No execution ledger
or authorization was created, so order restart-recovery verification was not
applicable. This result is not online order acceptance or partial execution.

## Unapproved replacement candidate

A separate approval could retain BUY LIMIT GTC **0.04 NVDA @ 165.60 USDT**,
BOTH, reduceOnly=false, with one POST, at most one known-order DELETE,
600 seconds, `CANCEL_KNOWN_THEN_STOP`, and `GET_RECONCILE_NO_RESUBMIT`.
At this observation, a candidate ask ceiling of **170.00 USDT** implies a
**6.80 USDT** full-notional cap and a rounded **6.81 USDT** margin budget
(`6.80 × 1.001 = 6.8068`). The original loss-threshold calculation remains
within **0.22 USDT** (`6.624 × 3% + 2 × 6.80 × 0.10% = 0.21232`).

These are proposed bounds only, not renewed authorization. A replacement
approval must specify its absolute expiry and exact client order identity,
retain the complete original account binding, and pass fresh server-time,
market, account, filter, order-history, exact-ID, and journal checks. No new
expiry or write capability was assigned in this attempt. The original price,
quantity and limits were not changed for execution.
