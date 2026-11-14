# Binance Demo online acceptance, 2026-11-14

**DEMO_ORDER_REJECTED_DIAGNOSED / NO LIVE MONEY**

Windows Credential Manager saved the Demo key pair and complete private account
binding after eight successful setup GETs. Readback in a separate process
confirmed the same binding without credential entry. The Demo account reported
`canTrade=true`, one-way mode, and NVDAUSDT at 1× CROSSED.

The smoke test sent `BUY LIMIT GTC` through the shared controller, Demo gateway,
adapter and fixed-host transport. It used `NVDAUSDT`, `positionSide=BOTH`,
`reduceOnly=false`, quantity `0.04`, and an exchange-tick price of `162.73000 USDT`
from the fresh ask. Order notional at the limit was `6.5092 USDT`.

| Evidence | Observed result |
| --- | --- |
| Demo host | `https://demo-fapi.binance.com` |
| Client order ID | `fl-7002aff6a4cb475f-01` |
| Exchange response | HTTP 400, `-4411`, `Please sign TradFi-Perps agreement contract fapi.` |
| Local outcome | `REJECTED`, category `EXCHANGE_REJECTED`, event `reject:fl-7002aff6a4cb475f-01` |
| Exchange order ID | Absent |
| Executed quantity and fee | `0` and `0 USDT` |
| Cleanup | No cancellation or reduction order was sent. |
| Fresh process | `DEMO_RECONCILED`. Local terminal rejection and its diagnostic were retained. Signed account reads confirmed no active order, no position and no unresolved UNKNOWN. |
| Setup + smoke test + fresh process | `GET=160`, `POST=1`, `DELETE=0`. The fresh process made 38 GETs. |

## Separately approved run after agreement signing

The operator reported signing the TradFi-Perps agreement and explicitly
authorized one more bounded test. The program loaded the same saved credential
and used a separate one-time budget. It sent `BUY LIMIT GTC`, quantity `0.04`,
at the fresh ask-derived limit of `162.82000 USDT` (`6.5128 USDT` notional).

| Evidence | Observed result |
| --- | --- |
| Client order ID | `fl-cd01d35a5c10454a-01` |
| Exchange response | HTTP 400, `-4411`, `Please sign TradFi-Perps agreement contract fapi.` |
| Local outcome | `REJECTED`, category `EXCHANGE_REJECTED`, event `reject:fl-cd01d35a5c10454a-01` |
| Exchange order ID | Absent |
| Executed quantity and fee | `0` and `0 USDT` |
| Fresh process | `DEMO_RECONCILED`. Both terminal rejection records were retained. There were no active orders, positions or unresolved UNKNOWN. |
| Second run, including fresh process | `GET=152`, `POST=1`, `DELETE=0`. The fresh process made 38 GETs. |
| Total | `GET=312`, `POST=2`, `DELETE=0`. |

Both one-time entry attempts were spent. The runtime ended disabled. The actual
signed Demo request still received `-4411` after the reported signing. Targeted
tests with simulated responses cover fills, reduce-only closing, restart fee
reconciliation, GET-only recovery after a lost POST reply, and durable stop and
approval boundaries.

## Signed Demo TradFi API request

The operator reported signing the TradFi-Perps agreement on the Demo site and
successfully trading in the browser. They explicitly authorized one API signing
attempt on the same locally bound Demo credential. The CLI checked the full
binding, synchronized time, and completed a signed Demo account read before
calling the agreement endpoint.

| Evidence | Observed result |
| --- | --- |
| Request time | `2026-11-14T10:25:59.590154Z` (18:25:59 Beijing) |
| Endpoint | `POST https://demo-fapi.binance.com/fapi/v1/stock/contract` |
| Masked local account binding | `…f216660da077` |
| HTTP | `200` |
| Binance code / message | No parseable code or message recorded. |
| Result | `OUTCOME_UNKNOWN`. The CLI did not obtain the documented JSON acknowledgment `code=200`, `msg=success`. |
| Order operations | No order POST or DELETE. The new smoke budget was not consumed. |
| Independent GET recovery | `DEMO_RECONCILED` at `2026-11-14T10:26:55.761408Z`. Wallet and available balance were each 5,000 USDT. No active orders, positions or unknown orders. The two historical `-4411` rejections remained terminal. |
| Requests in this follow-up | `GET=40`, agreement `POST=1`, order `POST=0`, `DELETE=0`. |

The original agreement receipt retained the HTTP status and sanitized diagnostics.
It did not retain the body or its length. Future agreement receipts distinguish
empty, JSON and unparsed bodies without logging raw contents. Repeating the
command for this account returns its saved receipt.

### Binance technical support handoff

Please confirm whether `POST /fapi/v1/stock/contract` on `demo-fapi.binance.com`
updates the USD-M Demo API TradFi-Perps entitlement, and what response confirms
that it has been applied. The operator reports that NVDAUSDT works in the Demo
web interface after signing. For the credential with local masked binding
`…f216660da077`, signed account GETs succeeded, while order POSTs at about
`10:05Z` and `10:13Z` on 2026-11-14 both returned HTTP 400 and `-4411`.
The signed agreement POST at `10:25:59.590154Z` then returned HTTP 200 with no
parseable application confirmation. Please verify the agreement state for the
Demo API account and clarify whether the web and API share that state.

## P7-M07: API order after the agreement request

**DEMO_ORDER_ACCEPTED / DEMO_FILL_RECONCILED / NO LIVE MONEY**

The stored credential matched the full account binding from the agreement
request (`…f216660da077`). The user authorized one new BUY and at most one
cancellation of that known order. The test used a fresh one-POST budget, a
unique client order ID, and a limit derived from the current ask. The original
agreement receipt remains `OUTCOME_UNKNOWN`. The test omitted
`--require-tradfi-signed` and made no new agreement request.

| Evidence | Observed result |
| --- | --- |
| Scope | `https://demo-fapi.binance.com`, `NVDAUSDT`, `BUY LIMIT GTC`, `BOTH`, `reduceOnly=false` |
| Order | `0.04` at `162.94 USDT`, `6.5176 USDT` notional |
| Exchange response | HTTP 200, `FILLED`, no Binance error code |
| Order identity | `orderId=191862895`, `clientOrderId=fl-6134cc5d117d4d20-01` |
| Exchange fill time | `2026-11-14T11:15:52.715Z` (19:15:52.715 Beijing) |
| Confirmed execution | `0.04` at average `162.94 USDT`, `0.00162940 USDT` commission |
| Signed account snapshot | `2026-11-14T11:17:58.677854Z`. Wallet `4999.99837060 USDT`, available `4993.47853546 USDT`. |
| Remaining exposure | Long `0.04 NVDAUSDT` at entry `162.94 USDT`, 1× CROSSED. No active orders or unresolved order UNKNOWN. |
| Cleanup | The order was already filled. No cancellation or reduction was sent. The one-POST budget was consumed and the runtime ended disabled. |
| Request counts | `GET=158`, order `POST=1`, agreement `POST=0`, `DELETE=0` across execution and recovery. |

The original process and its first restart recovered the fill and commission,
but the account projection expected `entryPrice` in Account Information V3.
The fix uses the value from Position Information V3 when symbol, position side,
and quantity match the account row. The CLI reports the average execution price
from the journaled cumulative quote and filled quantity. A new process then
reconciled the same order, trade fees, position and wallet using 42 GETs. The
wallet decrease from 5,000 USDT matches the confirmed commission. The targeted
runtime and protocol suites passed 55 tests, including the observed V3 response
shape. Those tests use synthetic responses. The order and recovery above are
from Binance Demo.

The post-agreement order was accepted, so `-4411` did not block this test. The
two older `-4411` refusals remain in the journal.

## P7-M08: Reduce-only close and round-trip reconciliation

**DEMO_REDUCE_ONLY_FILLED / DEMO_POSITION_FLAT / DEMO_ROUND_TRIP_RECONCILED / NO LIVE MONEY**

The same bound Demo account (`…f216660da077`) reported a `0.04` NVDAUSDT long
at `162.94 USDT`, `ONE_WAY`, 1× CROSSED, with no active orders or unresolved
UNKNOWN. A new persistent campaign allowed one POST for 900 seconds. The
acceptance invocation used the existing runtime, controller, gateway, adapter
and Demo transport. Its dispatch guard required the exact authorized
reduce-only order and rechecked the current long before sending.

| Evidence | Observed result |
| --- | --- |
| Close request | `SELL LIMIT GTC`, `0.04 NVDAUSDT` at `162.30 USDT`, `positionSide=BOTH`, `reduceOnly=true`, no `closePosition` parameter |
| Exchange response | HTTP `200`, `FILLED`, no Binance error code |
| Close identity | `orderId=191865793`, `clientOrderId=fl-75475821429342b2-1000911ffbc2` |
| Close execution | `2026-11-14T11:30:11.319Z` (19:30:11.319 Beijing), `0.04` at average `162.30 USDT`, notional `6.4920000 USDT` |
| Order budget | `GET=182`, order `POST=1`, agreement `POST=0`, `DELETE=0`. The close filled immediately. |
| Independent process | `DEMO_RECONCILED`. Open and close orders were both `FILLED`. The account was flat, with no active orders or unresolved order UNKNOWN. |
| Ending balances | At `2026-11-14T11:32:00.531732Z`, wallet and available balance each `4999.97114760 USDT`. |
| Authorization state | `DEMO_STOPPED`. The one-POST budget was consumed. |

| Round-trip amount | USDT |
| --- | ---: |
| BUY commission, trade `39911928` | `0.00162940` |
| SELL commission, trade `39913139` | `0.00162300` |
| Total commission | `0.00325240` |
| Binance SELL `realizedPnl` | `-0.02560000` |
| Price P&L, `(162.30 - 162.94) × 0.04` | `-0.02560000` |
| Net P&L after both commissions | `-0.02885240` |

The signed trade list confirms both executions and commissions. The signed
income query records the realized P&L and the SELL commission, with no funding
or other cash movement in the queried interval. The BUY commission is confirmed
by its trade row and the post-entry wallet. The wallet moved from `5000.00000000`
before entry to `4999.99837060` before the close and `4999.97114760` after
the close. Each order has one positive execution delta in the controller journal
after restart, matching its Binance trade quantity, quote amount and fee.
