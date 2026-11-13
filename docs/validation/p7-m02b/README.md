# P7-M02B Official Demo account and plan review

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

The [C1 approval plan](C1.md) supersedes the expired LIMIT candidate below.

## P7-M02B-R leverage recheck and first LIMIT candidate

**2026-11-13 04:01:29.102 UTC** · **Official USD-M Demo** · **NVDAUSDT**

The signed Demo symbol configuration returned **1×** after the operator's
manual change. The first LIMIT candidate falls in the **[0, 10,000) USDT** tier,
which allows up to **10×** and has a **5%** maintenance margin rate.
`review_opening_plan()` returned `{"LIMIT": ()}`. The original
`RISK_BRACKET_LIMIT` is cleared at this snapshot.

- `P7_M02B_DEMO_ACCOUNT_PREFLIGHT_PASS`
- `P7_M02B_BOUNDED_PLAN_READY_FOR_APPROVAL`

| Account or market input | Result | Official Demo GET source |
| --- | --- | --- |
| Symbol, contract and configuration | NVDAUSDT `TRADING`, `TRADIFI_PERPETUAL`, USDT quote and margin. Leverage **1×**, `CROSSED`. | `/fapi/v1/exchangeInfo`, `/fapi/v1/symbolConfig` |
| Position mode and account trading flag | `ONE_WAY`, `canTrade=true`. V3 omitted the flag and V2 supplied it. | `/fapi/v1/positionSide/dual`, `/fapi/v2/account` |
| Key and permissions | Signed `USER_DATA` verified. The operator confirmed the Futures permission in the web UI. `TRADE_PERMISSION_API_UNVERIFIED`. | `/fapi/v3/account` and other signed GETs |
| Wallet and available balance | **5,000.00000000 USDT** each. Initial margin and unrealized P&L were **0 USDT**. | `/fapi/v3/account` |
| Existing exposure | **0** nonzero positions and **0** open orders across the account, including NVDAUSDT. Account and position risk agreed. | `/fapi/v3/account`, `/fapi/v3/positionRisk`, `/fapi/v1/openOrders` |
| Quantitative restrictions | Valid response. ACCOUNT and NVDAUSDT indicators `NOT_REPORTED`, with no reported applicable lock. | `/fapi/v1/apiTradingStatus` |
| LIMIT rules | LOT_SIZE 0.01 to 6,000 in steps of 0.01. MIN_NOTIONAL 5 USDT. PRICE_FILTER 0.01 to 20,000 in ticks of 0.01. PERCENT_PRICE 0.97 to 1.03 × mark. | `/fapi/v1/exchangeInfo` |
| Other exchange limits | 200 open orders allowed by the exchange. POSITION_RISK_CONTROL `NONE`. All returned filters were supported. | `/fapi/v1/exchangeInfo` |
| Leverage tiers | Maximum leverage 10×, 5×, 4×, 3×, 2× and 1× over the ranges listed below. The signed response omitted `notionalCoef`. | `/fapi/v1/leverageBracket` |
| Mark and book | Mark **164.00** at **04:01:26.591 UTC**. Bid **163.91**, ask **164.24** at **04:01:27.723 UTC**. Mark band **159.08–168.92 USDT**. | `/fapi/v1/premiumIndex`, `/fapi/v1/ticker/bookTicker` |

### Historical LIMIT candidate (expired at 04:11:29 UTC)

| Parameter | Candidate |
| --- | --- |
| Account and instrument | `DEMO`, NVDAUSDT, 1× `CROSSED`, `ONE_WAY`. The plan and account facts used the identity derived from the current Demo credential. |
| Order | **BUY LIMIT GTC, 0.04 NVDA at 159.58 USDT**, one-way `BOTH`, opening order. |
| Maximum quantity | **0.04 NVDA**. At 159.58, 0.03 NVDA is 4.7874 USDT and 0.04 NVDA is **6.3832 USDT**. |
| Maximum notional and margin | **6.57 USDT each**. Full opening reservation at the higher of the limit and ask is `0.04 × 164.24 = 6.5696 USDT`, rounded up to cents, with zero existing margin. |
| Shared controller limits | **6.57 USDT** each for order notional, instrument exposure and account budget, with **1 order per minute** and a **5-second** quote age limit. The 0.10% internal fee reserve brings required available collateral to **6.5761696 USDT**. `TradingController.assess()` returned `APPROVED` in memory with the observed quote and available balance. |
| Maximum loss | **0.21 USDT** proposed. Round up `6.3832 × 3% + 2 × 6.57 × 0.10% = 0.204636 USDT`. The 3% is an adverse move from the limit using the observed lower-band multiplier as a stress assumption. The two fee allowances use the controller's internal rate. |
| Count and duration | **1 total order**, **1 simultaneous open order**, `max_seconds=600`, absolute expiry **2026-11-13 04:11:29 UTC**. The earlier of UTC expiry and 600 seconds from capability creation controls writes. |
| Client order ID | Prefix `m02br-a93403dbe3cd01`, full ID `m02br-a93403dbe3cd01-01`. Signed `/fapi/v1/allOrders` returned zero NVDAUSDT rows and no prefix matches. The prefix had no match in the repository validation records. |
| Cleanup and recovery | `CANCEL_KNOWN_THEN_STOP` and `GET_RECONCILE_NO_RESUBMIT`. |

The proposed BUY sits below the observed bid and ask and inside the mark band.
It uses a tick-aligned price with 0.50 USDT of room above the observed lower
band. The candidate uses GTC, so a subsequent market move can fill it and a
capability expiry leaves any open exchange order in place. The loss threshold
gates opening admission based on account equity. After a fill, it cannot force
an exit or cap the loss on the resulting position.

Before an independently approved session, refresh the mark, book, account,
rules and actual controller journals. Check the prefix against the selected
protocol and controller journals, including terminal orders, and reconcile any
UNKNOWN identity. This observation found no Demo execution journal in the
reviewed worktree or local run directories. The exchange order-history result
covers the endpoint's retention window. The plan's bounds and prefix reside in
`DemoExecutionPlan`. Approval must also cover the exact side, limit and full
client ID in the order intent. An expired candidate or a changed price,
quantity or limit needs a refreshed plan for review. [Binance All Orders](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade#all-orders-user_data)

During an authorized session, the adapter journals the ID before dispatch.
Query the same ID with signed GET. Following a submission or cancellation
timeout, reconcile that identity with GET and stop new submissions while its
state is unknown. With separate approval covering cancellation, cancel only
the known outstanding quantity under the same ID while the capability remains
active. Query the order again and reconcile open orders, fills, fees, positions
and controller state. A fill can leave a residual Demo position after
cancellation. Stop and report it for separate review. After expiry, GET remains
available and an open order requires fresh cancellation authorization.

### Request receipt and verification

All exchange requests went to `https://demo-fapi.binance.com`.

| Round | Requests | HTTP result |
| --- | --- | --- |
| 03:58:20 UTC observation | 4 public GETs (time, exchangeInfo, mark, book) and 2 signed GETs (leverageBracket, account V3) | 4 × 200, 2 × 400 (`-1021 CLOCK_SKEW`) |
| 04:01:29 UTC observation and prefix check | 4 public GETs (time, exchangeInfo, mark, book) and 9 signed GETs (leverageBracket, account V3, positionRisk, position mode, openOrders, symbolConfig, account V2, apiTradingStatus, allOrders) | 13 × 200 |
| **Total** | **19 GETs (8 public and 11 signed), 0 POST, 0 DELETE, 0 other methods** | **17 × 200, 2 × 400** |

The successful read-only process subtracted 2,500 ms from its measured signing
offset and used a 10,000 ms `recvWindow` after the initial timestamp rejection.
It used the unadjusted midpoint offset of −457 ms to check market observation
age. Signed account facts, rules and quotes from that process supplied both plan
reviews. These signing settings were local to the read-only observation.
[Binance USD-M Demo endpoint and timestamp rules](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)

**94 targeted tests passed** with Python 3.12.7 and pytest 7.4.4 with
`-p no:nose` and socket connects blocked. The flag prevents pytest 7 from
calling ordinary `setup` helpers as module fixtures. The first two invocations
each reached 67 passes and 27 fixture setup errors before the compatibility flag
was applied. The passing suite covered the Production write refusal, independent
Demo capability, identity and credential binding, shared risk and limit checks,
durable identity, duplicate and UNKNOWN order handling, GET-only timeout
recovery, and capability expiry and restart.

The verified PR #131 head was
`69da2358d96dea375d3fd9535fa627146e659f27`. GitHub merged it at
**03:51:18 UTC** as `900eec8dd944513353f122c7b92f4ad5baf5a1ad` and the
original remote branch was deleted. [PR #131 CI and merge gate](https://github.com/Wind-Maker1001/FactorLab/actions/runs/43358202764)
This evidence update was recorded in the original local worktree. GitHub's
successful checks apply to the merged PR head. Execution admission remains
blocked pending independent approval.

## Historical baseline at 02:55 UTC

`P7_M02B_DEMO_READ_ONLY_VERIFIED` at **2026-11-13 02:55:55 UTC**.
The opening plan is blocked. Demo reported NVDAUSDT configured at **20×** and
reported a maximum of **10×** for the tier that would contain either minimum
opening order. With those inputs the existing validator returns
`RISK_BRACKET_LIMIT`. Account configuration changes are outside this session.
The plan limits and independent execution approval are pending.

| Observation | Result | GET source | Opening check |
| --- | --- | --- | --- |
| Environment and clock | Official USD-M Demo, midpoint time offset −508 ms | `/fapi/v1/time` | Observed |
| Account and key | Demo credential binding and USER_DATA verified. Account `canTrade=true`. Key TRADE permission UNKNOWN. | `/fapi/v3/account`, `/fapi/v2/account` | Account flag met. Key scope pending. |
| Quantitative status | ACCOUNT and NVDAUSDT indicators NOT_REPORTED in a valid response | `/fapi/v1/apiTradingStatus` | No reported applicable lock |
| Position and margin mode | ONE_WAY, CROSSED | `/fapi/v1/positionSide/dual`, `/fapi/v1/symbolConfig` | Supported |
| Wallet and available USDT | Read and positive. Initial margin and unrealized P&L were zero. | `/fapi/v3/account` | Observed. Compare to approved caps at execution. |
| Positions and open orders | Zero positions and zero open orders across the account | `/fapi/v3/account`, `/fapi/v3/positionRisk`, `/fapi/v1/openOrders` | Clear at observation |
| Contract | NVDAUSDT TRADING, TRADIFI_PERPETUAL, USDT quote/margin, LIMIT and MARKET available | `/fapi/v1/exchangeInfo` | Supported |
| LOT_SIZE | Min 0.01, max 6,000, step 0.01 | `/fapi/v1/exchangeInfo` | Candidate must align |
| MARKET_LOT_SIZE | Min 0.01, max 600, step 0.01 | `/fapi/v1/exchangeInfo` | Candidate must align with both quantity grids |
| MIN_NOTIONAL | 5 USDT | `/fapi/v1/exchangeInfo` | Sizes below 5 USDT blocked for an opening |
| PRICE_FILTER | 0.01 to 20,000 USDT, tick 0.01 | `/fapi/v1/exchangeInfo` | LIMIT must align |
| PERCENT_PRICE | 0.9700 to 1.0300 × mark | `/fapi/v1/exchangeInfo` | 161.0022846669 to 170.9611888731 USDT at observation |
| MAX_NUM_ORDERS | 200 | `/fapi/v1/exchangeInfo` | Exchange capacity available |
| POSITION_RISK_CONTROL | NONE | `/fapi/v1/exchangeInfo` | No reported restriction |
| Leverage | Configured 20×. Tier caps 10×, 5×, 4×, 3×, 2×, 1×. | `/fapi/v1/symbolConfig`, `/fapi/v1/leverageBracket` | **Blocked** |
| Mark and book | Mark 165.98173677 at 02:55:53.592 UTC. Bid 165.91 and ask 166.26 at 02:55:54.903 UTC. | `/fapi/v1/premiumIndex`, `/fapi/v1/ticker/bookTicker` | Fresh at observation |

Reported leverage tiers use inclusive notional floors and exclusive caps.

| Notional range in USDT | Maximum initial leverage | Maintenance rate |
| --- | ---: | ---: |
| 0 to 10,000 | 10× | 5% |
| 10,000 to 50,000 | 5× | 10% |
| 50,000 to 100,000 | 4× | 12.5% |
| 100,000 to 250,000 | 3× | 16.67% |
| 250,000 to 2,500,000 | 2× | 25% |
| 2,500,000 to 5,000,000 | 1× | 50% |

### Original pending DemoExecutionPlan

| Parameter | Proposed value or decision required |
| --- | --- |
| `environment`, `symbol`, `account_id` | DEMO, NVDAUSDT, the exact Demo credential binding in the operator's local report |
| `order_types` | Proposed C1 scope `{'LIMIT'}`. C2 MARKET requires explicit inclusion. |
| `max_quantity` | Pending approval. The observed opening minimum was 0.04 NVDA. |
| `max_notional` | Pending approval. See candidate reservations below. |
| `max_orders`, `max_open_orders` | Pending approval. A plan including C1 and C2 needs at least two total orders. The simultaneous cap must be positive and no greater than the total cap. |
| `max_margin`, `max_loss` | Pending approval in USDT. The gateway reserves full opening notional and also checks existing collateral and orders. |
| `max_seconds`, `expires_at` | Pending approval. Supply maximum duration and a future UTC expiry. |
| `client_id_prefix` | Proposed C1 prefix `m02b-50017a4d2e0b49`, subject to review against the journals. The later strategy deployment requires a unique prefix of 1–6 characters. |
| `cleanup`, `recovery` | `CANCEL_KNOWN_THEN_STOP`, `GET_RECONCILE_NO_RESUBMIT`. C1 cancellation uses the same order identity while the approved capability remains active. |
| Strategy and model | Later Phase D proposal is `dual_ma` version `1`, 5/20 windows with 21 closed bars, 1h interval, target weight pending review. Frozen bundle not applicable. |
| Price and bar sources | C1 Demo REST mark and top of book. For Phase D the proposed source is the official Demo `nvdausdt@kline_1h` stream on `wss://demo-fstream.binance.com`. Confirm the feed and bind source, closed-bar time, signal, target and client ID before Phase D. |
| Shared controller limits | Pending review of order and instrument notional, account budget and order rate. The controller reserves a fee budget in addition to notional. |

At a hypothetical LIMIT price of 165.98 USDT, 0.03 NVDA yields 4.9794 USDT, so the
first admissible quantity on that grid is 0.04 NVDA and 6.6392 USDT. The gateway
would reserve 6.6504 USDT against the plan at the observed ask of 166.26. For
MARKET the same quantity at the observed mark represents 6.6392694708 USDT.
The gateway tests the MARKET plan cap against the upper mark band, which puts the
reservation at 6.838447554924 USDT. These are sizing calculations for the observed
prices. The operator must choose and approve the actual side, limit price, fill
exposure and caps using fresh evidence.

The Phase D runtime emits MARKET orders. A C1 LIMIT-only plan covers the manual
placement and cancellation procedure. A later strategy plan needs its own
approved limits, feed binding and short client ID prefix. A verified Demo software
path carries no model-performance claim. [Binance USD-M Demo host and security types](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)

### Original evidence

The credentialed observation made 12 HTTP 200 GETs to `https://demo-fapi.binance.com`.
Four were public (`/fapi/v1/time`, `/fapi/v1/exchangeInfo`,
`/fapi/v1/premiumIndex`, `/fapi/v1/ticker/bookTicker`). Eight were signed
(`/fapi/v1/leverageBracket`, `/fapi/v3/account`, `/fapi/v3/positionRisk`,
`/fapi/v1/positionSide/dual`, `/fapi/v1/openOrders`, `/fapi/v1/symbolConfig`,
`/fapi/v2/account`, `/fapi/v1/apiTradingStatus`). An earlier unsigned observation
used time, exchangeInfo and premiumIndex. The first local test attempt used the
system pytest 7, whose legacy `setup` hook caused two additional Demo GETs before
the HTTP stub took effect. ExchangeInfo returned data and leverageBracket rejected
the synthetic test key with HTTP 401. The helper was renamed before the final
offline run. **Actual Demo POST/DELETE count was zero.**

The final local suite used the existing Python 3.13.9 and pytest 8.4.2 with the
workspace `src` on PYTHONPATH and socket connections blocked in the test runner.
**206 tests passed** across Demo preflight, plan review, network, execution,
strategy, environment isolation and USD-M account recovery. The tests inject
account and execution responses. Shared restart recovery books a missed partial
fill and fee once, and the reopened path denies POST and DELETE before HTTP
dispatch. All order, fill and cancellation results in this suite are offline
evidence. [Binance USD-M account and leverage bracket contract](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/account)
