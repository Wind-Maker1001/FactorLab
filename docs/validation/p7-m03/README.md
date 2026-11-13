# P7-M03 official Demo opening-order review

**P7_M03_REAPPROVAL_REQUIRED**

## Authorized execution attempt

The exact proposed plan below received separate explicit approval. The subsequent
GET-only pre-write verification stopped at `APPROVED_PRICE_BOUNDARY`: the fixed
168.66 limit was not strictly below the refreshed bid, or the refreshed ask
exceeded 170.41. The combined check did not retain the rejected quote values;
the receipt does not establish which comparison failed.

The [online receipt](online-receipt.json) records **14 GET, 0 POST, 0 DELETE**.
The complete historical account binding matched, the new server-time midpoint
offset was **−1,282 ms**, and the exact client ID again returned `-2013`.
No write authorization or execution journal was created. No price, quantity,
budget, identity or expiry was changed, and no further execution was attempted.
The approved plan remains below as the historical proposal, not permission to
retry after this boundary failure.

Across preparation and this execution attempt, actual traffic was **40 GET,
0 POST, 0 DELETE**. There is no exchange order ID, no submitted order, no fill,
no commission and no position created by this task. The latest complete recorded
account observation remains the flat preparation account; the attempted run did
not save its later account/quote payload after rejecting the market boundary.
Economic execution-ledger reconciliation and restart recovery were not reached.
This is a pre-write block, not online order acceptance or partial execution.

A replacement plan needs fresh recorded market facts and separate explicit
approval. No automatic retry, renewal, market order or position close is enabled.


**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

Baseline: `9cbf6bb44b778bf87e1d8042fe298cfb340ad2da`.
The final GET-only observation completed at **2026-11-13 10:41:27.737728 UTC**.
The [opening review](opening-review.json) and [initial preflight](readonly-preflight.json)
contain sanitized facts and endpoint receipts. No network-write capability was created.

## Account and market evidence

The full credential-derived account ID matched the historical C1 binding exactly;
its public reference is `DEMO:FactorLab-Demo:sha256:...660da077`.
The full account binding and proposed plan are held locally in
`%LOCALAPPDATA%/FactorLab/demo/p7-m03/opening-plan.json`, without credentials.
Signed reads used `https://demo-fapi.binance.com` and a newly measured server-time
midpoint offset of **−684 ms**, without historical corrections.

The account reported `canTrade=true`, `ONE_WAY`, NVDAUSDT **1×**, and `CROSSED`.
Wallet, margin balance and available USDT were **5,000** each; unrealized P&L,
initial margin, maintenance margin and open-order margin were zero. The account
and position-risk sources agreed on zero nonzero positions; there were zero open
orders. Quantitative status was observed without an applicable reported lock;
ACCOUNT and NVDAUSDT indicator scopes were `NOT_REPORTED`.

Mark was **169.51** at 10:41:24.592 UTC; bid/ask were **169.51 / 169.56** at
10:41:24.399 UTC. Both observations passed the five-second clock-adjusted freshness
bound. The contract was `TRADING`, `TRADIFI_PERPETUAL`, quoted and margined in USDT.
LIMIT quantity minimum/step were **0.01 / 0.01 NVDA**, maximum **6,000**;
minimum notional **5 USDT**; price minimum/maximum/tick **0.01 / 20,000 / 0.01 USDT**.
The mark-price band was **164.4247–174.5953 USDT** (0.97–1.03 × mark).
The exchange order cap was 200 and position-risk control was `NONE`.
The applicable leverage bracket was **[0, 10,000) USDT**, maximum **10×**,
maintenance margin ratio **5%**; actual leverage remained 1×.

Signed NVDAUSDT `allOrders` returned zero rows in its query retention scope.
Exact-ID GET returned HTTP 400 / `-2013 ORDER_NOT_FOUND`. No matching identity
was present in the reviewed C1/M03 approval records. A depth-three scan of the
local Demo state root and the two predecessor worktrees' `local-runs`,
`local-artifacts`, and `local-tmp` roots found no SQLite protocol or shared-account
journals. Identity and journals must be checked again immediately before execution.

## Proposed complete plan

| Parameter | Proposed fixed value |
| --- | --- |
| Account | Full historical C1 account binding; public reference `...660da077` |
| `environment`, `symbol` | `DEMO`, `NVDAUSDT` |
| `order_types` | `{'LIMIT'}` |
| `side`, `positionSide`, `reduceOnly` | `BUY`, `BOTH`, `false` |
| `timeInForce` | `GTC` |
| `quantity`, `max_quantity` | **0.03 NVDA** each |
| `limit_price` | **168.66 USDT** |
| Explicit ask ceiling | **170.41 USDT** |
| `max_notional` | **5.12 USDT** |
| `max_margin` | **5.13 USDT** |
| `max_loss` | **0.17 USDT** |
| `max_orders`, `max_open_orders` | **1** each |
| `max_seconds` | **600**, from process-local capability creation |
| `expires_at` | **2026-11-13T12:41:27Z** |
| `client_id_prefix` | `m03-5589adc5a5e847f9` |
| Complete `clientOrderId` | `m03-5589adc5a5e847f9-01` |
| `cleanup` | `CANCEL_KNOWN_THEN_STOP` |
| `recovery` | `GET_RECONCILE_NO_RESUBMIT` |
| Controller limits | Order notional, instrument exposure and account budget **5.12 USDT** each; **1 order/minute**, **5-second** quote age; **0.10%** fee reserve |
| Write bounds after separate approval | At most **1 LIMIT POST**, at most **1 DELETE for this exact known order ID**; no replacement, resubmission, MARKET order or automatic position close |

The earlier of absolute expiry and 600 seconds ends write authority. No automatic
renewal or budget increase is permitted. Execution must retain the exact account,
order identity, quantity and price, keep the limit strictly below the refreshed bid,
pass current exchange filters and mark band, keep the ask at or below 170.41,
retain flat account state and the stated configuration, and pass both admission
layers. Any failed boundary requires stopping for renewed approval.

## Sizing, fees and margin

The limit is `floor(169.51 × 0.995 / 0.01) × 0.01 = 168.66`.
It is 0.85 below the bid and 4.2353 above the observed lower band.
At this limit 0.02 NVDA is 3.3732 USDT, below minimum; **0.03 NVDA is the minimum**
legal size at 5.0598 USDT. Increasing to 0.04 is unnecessary.

| Amount | Calculation in USDT |
| --- | --- |
| Order notional / nominal 1× initial margin before fees | `0.03 × 168.66 = 5.0598` |
| Observed gateway full-notional occupancy | `0.03 × max(168.66, 169.56) = 5.0868` |
| Observed controller fee reserve | `5.0868 × 0.001 = 0.0050868` |
| Observed controller total reserve | `5.0868 × 1.001 = 5.0918868` |
| Fixed ask ceiling | Round `169.56 × 1.005` upward to cents: **170.41** |
| Gateway occupancy at ask ceiling | `0.03 × 170.41 = 5.1123`, rounded up to **5.12** |
| Controller total reserve at ask ceiling | `5.1123 × 1.001 = 5.1174123` |
| Margin budget | Round `5.12 × 1.001 = 5.12512` upward to **5.13** |
| Loss admission threshold | Round `5.0598 × 0.03 + 2 × 5.12 × 0.001 = 0.162034` upward to **0.17** |

The internal fee allowance is not a claim about actual exchange commission.
The 0.17 loss threshold gates new opening admission; it is not a stop-loss or a
maximum realizable loss. A filled position is retained, including after expiry;
closing it requires a separate reduce-only plan. A GTC order can remain open if
cleanup cannot finish before authorization expiry.

## Offline admission and request receipt

`review_opening_plan()` returned `{"LIMIT": []}`: no blockers.
The existing `TradingController.assess()` returned **APPROVED** for the exact
intent at the observed ask and again at the fixed ask ceiling. The controller
used an in-memory `PaperStore` and `PaperBroker`; no offline submit or network
writer was needed. The observed rules separately validated the minimum-size
intent through the plan review. No trading code was changed.

Actual task traffic: **26 GET, 0 POST, 0 DELETE**: 12 GET for the initial
preflight, 14 GET for the final account/identity/plan observation. Twenty-five
returned HTTP 200; the exact-ID absence query returned HTTP 400 / `-2013`.
A preliminary bare-runtime launch failed on missing pandas before any request;
the configured Python 3.13 environment completed both subsequent runs.

There is no exchange order ID or submitted order state. Actual submitted volume
and fees are zero; both account observations were flat with 5,000 available USDT.
Execution-ledger reconciliation and restart recovery belong to the separately
authorized online order, not to this preparation. The outstanding execution risk
is the first trade-permission confirmation and possible fill/residual exposure;
key-level TRADE permission remains unproven by GET alone.

Validation: real signed GET observations, successful preparation plan review,
two successful shared-controller offline assessments, and the subsequent
pre-write market-boundary rejection. This evidence-only change does not alter
trading code; no repository test matrix was run. Online acceptance has not occurred.
