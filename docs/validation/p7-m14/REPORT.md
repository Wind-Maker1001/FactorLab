# P7-M14 — Demo historical reconciliation

Baseline: `d9b608d` (origin/main at worktree creation).

## Cost and authority

`FuturesAdapter.reconcile()` closes the global risk gate, visits each persisted protocol order, calls `query_order()` → `/fapi/v1/order` → `_rest_order()`, then reads current account facts. Only locally confirmed REJECTED orders without an exchange ID are excluded. CANCELED is not final for this purpose: the shared state machine permits a subsequent FILLED confirmation. Filled quantities require order-specific `/fapi/v1/userTrades`; their quantity and quote totals must equal the order cumulative totals, and commission must be USDT.

`FuturesPaperGateway.recover()` first reconciles the adapter, replays confirmed missing economic deltas through the existing Controller journal, then invokes Controller reconciliation, which invokes adapter reconciliation again. The two reads bracket replay and account conservation checks. The #162 idle benchmark therefore makes two sets of six authenticated account reads plus one market quote per recovery.

Authoritative responsibilities:

- `/fapi/v1/order`: journaled client identity, exchange order identity, status, cumulative execution quantity and quote.
- `/fapi/v1/userTrades`: actual execution identities and commissions, checked against order totals. Existing behavior rejects duplicate IDs and a full 1000-row response; it does not claim paginated completeness.
- `/fapi/v1/income`: account activity, including funding; not an order-status or order-cumulative substitute.
- `/fapi/v1/openOrders`: current unmanaged-order detection, not historical finality.
- `/fapi/v3/account`, `/fapi/v3/positionRisk`, `/fapi/v1/positionSide/dual`, `/fapi/v1/symbolConfig`, `/fapi/v1/apiTradingStatus`: account balances/positions, one-way mode, margin configuration and quantitative admission. Controller reconciliation compares exchange and local balances/positions.

Existing persisted identities are protocol account binding, client/exchange order IDs, raw REST response event hashes, stream execution identities and Controller recovery fingerprints. There is no persisted, independently verifiable complete historical REST cursor. A new cursor cannot erase old conflicting evidence.

## Baseline measurements

Same existing #162 HTTP-stubbed benchmark, Windows CPython 3.12.7:

| Scenario | GET | Endpoint counts | Seconds |
| --- | ---: | --- | ---: |
| 100 canceled orders, adapter reconciliation | 106 | order 100; six account endpoints once each | 3.188 |
| 1000 canceled orders, adapter reconciliation | 1006 | order 1000; six account endpoints once each | 30.614 |
| 20 idle Grid recoveries | 260 | six account endpoints 40 each; bookTicker 20 | 2.411 |

Idle replay added zero Controller events and preserved economic state across recovery and restart. Historical SQLite totals were 258048/1552384 bytes; idle total was 114688 bytes. These are local fixture timings, not network latency estimates.

## Completeness decision

**INCREMENTAL_RECONCILIATION_BLOCKED**: the verified official material supplies retrieval parameters, not a guarantee that all later historical corrections appear after a trade cursor. No cross-round cursor or terminal-order skip is introduced.

The official [USD-M connector account source](https://raw.githubusercontent.com/binance/binance-futures-connector-python/main/binance/um_futures/account.py) documents:

- userTrades defaults to the most recent seven days, permits at most seven days between start/end, caps limit at 1000 and forbids combining fromId with time bounds. A trade-ID cursor is not a correction watermark.
- income defaults to recent seven days, has inclusive start/end, page and limit (maximum 1000); transaction identity is unique within incomeType, not globally across all types.
- allOrders returns active/canceled/filled orders, with symbol/orderId/time/limit parameters. The retrieved connector does not establish field parity, atomicity or equal freshness with Query Order.
- openOrders is a current open-order list, not historical completeness evidence.

The current developers.binance.com pages could not be retrieved. Exact current retention thresholds, pagination inclusivity for trades and commission correction publication semantics are therefore not established. No retention threshold is used as finality. Connector [user-stream lifecycle documentation](https://raw.githubusercontent.com/binance/binance-futures-connector-python/main/binance/um_futures/data_stream.py) does not establish durable replay after disconnect.

A full per-round allOrders batch with individual fallback for omitted identities is a separate possible optimization, but field parity and endpoint authority would first need verification. It is not enabled by this delivery.

## Recovery argument

The existing protocol remains the source of truth; no migration, cursor, second economic ledger, index, duplicated history or economic algorithm is added. Confirmed cumulative quantity/quote never regress. Every persisted sent identity still receives authoritative lookup, even after cancellation or a missing ACK. A missing exchange order cannot authorize replacement. Locally unsubmitted intents are not exchange facts; a locally persisted dispatch placeholder is UNKNOWN and cannot be re-POSTed. NEW exchange orders and PARTIALLY_FILLED orders remain queryable and block uncertainty rather than being inferred from open-order absence.

Fresh order totals and actual trade totals must agree before fees are accepted. Duplicate trade IDs, a saturated page, missing trade quantity/notional, non-USDT commission and commission-only corrections fail closed. A forward cursor would hide some of those conflicts; preserving full reads keeps them observable. The shared state machine rejects contradictory statuses and fee changes without a fill. This blocks a correction instead of silently booking an unsupported economic event.

`test_demo_reconciliation_m14.py` adds canceled-to-filled restart/idempotence, missing/duplicate trades, fee asset/notional/correction conflicts, contradictory status, unordered trade rows, full-page incompleteness and persistent account binding cases. Existing dedicated futures/network tests cover lost ACK with one POST, order absence, partial disconnect, host/credential/account isolation, rule admission and Controller conservation.

The adapter does not itself reconcile income against a separate trade ledger. The gateway compares Controller assets and positions with current authoritative account facts; an unexplained income/balance change must leave Controller reconciliation false. No income cursor is claimed complete. The measured safe alternative is the already deployed #162 reuse of just-read facts within adapter reconciliation: 260 rather than 500 GET per 20 empty recoveries. This delivery does not claim additional GET savings.

## Reproduction

From the repository root, in the existing test environment with pytest and psutil:

```sh
python docs/validation/p7-m14/benchmark.py --output docs/validation/p7-m14/measurements.json
python -m pytest tests/integrations/test_binance_futures_trading.py tests/integrations/test_binance_demo_network.py tests/integrations/test_binance_demo_grid_network.py -q
```

The benchmark uses deterministic in-process protocol responses and the unchanged #162 HTTP fixtures. Full recovery scenarios use the same fixture on both sides of this comment/test-only change; no performance implementation is switched on. RSS is process resident memory before/after the measured interval, CPU is process CPU time, persistent growth includes SQLite sidecars, and replay state equality is asserted. Timings do not estimate hosted or network speed. No real credentials, external exchange reads or network writes are involved.

## Final results

**M14_ENGINEERING_ACCEPTED** — blocked-path engineering evidence complete.

**INCREMENTAL_RECONCILIATION_BLOCKED** — no safe incremental history-skipping contract established; necessary historical reads remain enabled.

The #162 HTTP benchmark comparison is:

| Scenario | Baseline GET | Delivery GET | Baseline seconds | Delivery seconds |
| --- | ---: | ---: | ---: | ---: |
| 100 canceled orders | 106 | 106 | 3.188 | 3.017 |
| 1000 canceled orders | 1006 | 1006 | 30.614 | 33.276 |
| 20 idle recoveries | 260 | 260 | 2.411 | 3.847 |

Endpoint counts are identical, including six account endpoints per adapter reconciliation. No new request reduction or latency improvement is claimed. The production change only clarifies the existing TODO; the executable recovery algorithm is identical to baseline.

Additional frozen full-gateway measurements exercise both authoritative reconciliation passes and Controller replay. They use an offline transport (five account endpoints, without Demo quantitative admission), not the HTTP fixture above, so their wall times are not comparable with that table:

| Frozen history / recovery | GET (order / userTrades / account total) | Wall s | CPU s | RSS before → after, bytes | Persistent growth, bytes | Controller / protocol events added |
| --- | --- | ---: | ---: | --- | ---: | --- |
| 100 orders, one healthy round | 210 (200 / 0 / 10) | 0.241 | 0.141 | 136364032 → 137068544 | 0 | 0 / 0 |
| 1000 orders, one healthy round | 2010 (2000 / 0 / 10) | 1.372 | 1.266 | 153595904 → 160997376 | 0 | 0 / 0 |
| 100 orders, 20 healthy rounds | 4200 (4000 / 0 / 200) | 5.016 | 3.078 | 157745152 → 157974528 | 0 | 0 / 0 |
| 100 orders, one late-fill recovery | 212 (200 / 2 / 10) | 0.469 | 0.125 | 157986816 → 158027776 | 40960 | 1 / 1 |

Each account endpoint (`openOrders`, `positionSide/dual`, `symbolConfig`, `account`, `positionRisk`) was read twice per full gateway round. Every scenario explicitly repeated recovery afterward and asserted identical persisted Controller economic state. Late fill booked the actual 2-unit execution and 0.2 USDT fee once. A separate restart test retained identical protocol quantity, quote and fee. Because executable recovery is unchanged, these CPU/RSS/persistence observations describe both baseline and delivery, not an optimized second algorithm. RSS includes retained fixture request objects and interpreter allocation, rather than isolating adapter working memory. The raw successful transcript is in `measurements.json`.

The final benchmark passed 18 tests, including 11 safety cases and four full-gateway cost scenarios. The dedicated futures, Demo network and Grid-network regression selection passed 135 tests on the first run; its remaining subprocess-isolation test initially lacked the worktree source path and passed when rerun with `PYTHONPATH="$PWD/src"`. An additional unexplained-income conservation test passed independently and is also included in the final 18-test run. The full-gateway benchmark initially attempted orphan-import of all historical orders simultaneously and hit collateral reservations; the corrected frozen fixture restores already-booked canceled orders on both sides before measuring. No production behavior was changed to accommodate either test setup issue.

NO LIVE MONEY. No Demo authorization or budget changes; no network trading operations; no history deletion.
