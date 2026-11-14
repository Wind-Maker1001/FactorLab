# P7-M10-A: official Binance Demo Grid order acceptance

The existing `tools/demo_grid_acceptance.py` completed a real Demo order lifecycle against `https://demo-fapi.binance.com`. These are exchange responses, not offline HTTP Stub results. This validates execution capability, not profitability.

[Structured lifecycle evidence](accepted-order.json) records the original configuration, per-process account observations and request counts, persistent POST usage, and write receipts. [Earlier blocked attempt and read-only diagnosis](online-acceptance.json) remain separate from the successful campaign.

## Accepted order

| Field | Observed value |
| --- | --- |
| Campaign | `demo-18a8dd9673194131bc2d9b2170731088` |
| Exchange orderId | `191944057` |
| clientOrderId | `fl-9624718bcfb044b8-g-00000001` |
| Instrument / side / type | NVDAUSDT / BUY / LIMIT GTC |
| Quantity | 0.04 NVDA |
| Original Grid limit / exchange-accepted limit | 158.63 / 158.63000 USDT |
| POST response | HTTP 200, NEW |
| Independent GET recovery | Same orderId, NEW; POST 0, DELETE 0 |
| Cancellation | HTTP 200, CANCELED |
| Subsequent independent GET recoveries | CANCELED, including after authorization stopped |
| Cumulative fill / fee | 0 NVDA / 0 USDT |
| Campaign order POST / DELETE count | 1 / 1 |

The new campaign used 3600 seconds, two order POST attempts, a 1 USDT loss cap, 0.04 NVDA maximum quantity, 7 USDT maximum notional, and one active-order slot. `submit --new-deployment` retained the previous campaign's journals. The BUY limit remained the Grid's original lower boundary; book and mark observations were risk inputs, not replacement prices.

The successful submit process reported GET 132 / POST 1 / DELETE 0. Independent pre-cancellation recovery reported GET 49 / POST 0 / DELETE 0. Cancellation reported GET 96 / POST 0 / DELETE 1. Each subsequent independent recovery reported GET 49 / POST 0 / DELETE 0. Persistent POST consumption remained one throughout recovery.

## Final account

`stop` returned `DEMO_STOPPED` with `runtime.enabled=false`. Final independent recovery reported `reconciled=true`, an inactive campaign, no positions, no active orders, and no UNKNOWN orders. Wallet and available balance were both 4999.97114760 USDT, unchanged from the preflight account.

An initial attempt in this round failed during read-only account preparation without creating a campaign or submitting an order. A bounded GET recovery succeeded before campaign creation continued. No uncertain POST was resent, no recvWindow was widened, and no order-state journal was reset.

No trading implementation changes were required. The real submission, independent recoveries and cancellation supply the execution evidence; no duplicate offline suite was run for this evidence-only change.

NO LIVE MONEY.
