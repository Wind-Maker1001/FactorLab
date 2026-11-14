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
