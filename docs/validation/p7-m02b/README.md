# P7-M02B Official Demo account and plan review

**NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED**

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

## Pending DemoExecutionPlan

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

## Evidence

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
