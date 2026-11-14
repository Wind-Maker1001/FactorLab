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

The test entry spent its one-time attempt and the runtime ended disabled. The
exchange requires completion of the TradFi-Perps agreement before this account
can trade NVDAUSDT. The targeted simulated-response tests exercise full-fill, reduce-only closing,
restart fee reconciliation and GET-only recovery after a lost POST reply.
