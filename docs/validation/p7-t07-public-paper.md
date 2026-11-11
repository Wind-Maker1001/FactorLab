# P7-T07 public-market paper acceptance

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

Baseline: `4497cb134acfddaa4bcd8aebd7d236ae13ab4b80` (PR #110).

## Separate outcomes

| Observation | Futures NVDAUSDT | Stocks NVDA |
| --- | --- | --- |
| Offline deterministic closure | PASS | PASS |
| Online public market acquisition | PASS | PASS |
| Online real-quote-driven paper order | NOT_VERIFIED | NOT_VERIFIED |
| Online simulated execution | NOT_VERIFIED | NOT_VERIFIED |
| Account/position persistence reconciliation | PASS | PASS |

The engineering acceptance marker is withheld: neither market produced an online
order or fill. NO_TRADE is the observed outcome, not a successful trading claim.

## Single online window

Executed the realtime CLI with explicit public data, paper simulation, acceptance
signal, rule refresh, 60 seconds and two reconnects. Each market made one socket
connection; no reconnect was needed. All `BINANCE_*` environment variables were
removed before constructing readers. Only public market IO occurred.

Recorded UTC timestamps are 2026-11-11T18:23:41Z–18:24:46Z, as returned by the
runtime clock; these are the evidence timestamps rather than the task label date.

| Measurement | Futures | Stocks |
| --- | --- | --- |
| Market messages (excludes subscription ACK) | 50 | 34 |
| Parsed events | 50: 22 mark, 28 trade | 33 quotes |
| Accepted normalized events | 9 (mark/trade combined) | 33 quotes |
| Public rules | VERIFIED, VALID | UNKNOWN; key-required reader raised ValueError |
| Public session | VERIFIED current TRADING status; schedule UNKNOWN | UNKNOWN; no calendar evidence |
| Strategy enabled at socket start | yes | no: rule refresh failure paused account |
| Strategy signals / orders / fills | 0 / 0 / 0 | 0 / 0 / 0 |
| Filled quantity | 0 | 0 |
| Synthetic fees / slippage paid | 0 USDT / 0 USDT | 0 USD / 0 USD |
| Final cash / reserved | 1000 USDT / 0 | 1000 USD / 0 |
| Final positions | empty | empty |
| Rejected observations | 41 stale-or-future | parse Unavailable on subsequent message |
| Final result | NO_TRADE, paused | NO_TRADE, paused |

Futures last accepted executable source time was 18:23:46.826Z. Subsequent invalid
observations paused execution before a 30-second test trigger could occur. Stocks
never obtained rules or initial calendar evidence. No synthetic session was
installed online. No rejection is attributed to a real account permission grant.
The online result does not separately count accepted Futures executable events;
its nine accepted events include mark updates.

The requested socket duration was 60 seconds. Measured Futures observer time was
62.77 seconds including synchronous event work and connection closure; Stocks
was 60.52 seconds. Total acquisition/observation/closure time was 66.673 seconds.
Peak process working set was 168,484,864 bytes (160.68 MiB). The two SQLite stores
totaled 5,357,568 bytes (5.11 MiB); no raw WebSocket archive was written.

The run exposed a full-universe exchangeInfo snapshot on the synchronous event
path. The final implementation retains only the requested symbol with explicit
returned/retained counts, while keeping the original endpoint, version and time
receipt. A targeted test verifies this reduction. The sole online run preceded
this reduction; its measured time/memory/disk are not final-code benchmarks.

A separate process reopened both online accounts using the same explicit policy.
Cash, reserved balances, positions, open orders, account identity and reconciliation
matched; both had zero orders. Public rule/session authority cleared on restart.
Nonempty order-identity recovery is established separately by offline subprocess
scenarios, not by this empty online ledger.

## Offline deterministic coverage

All offline prices, rules and sessions in deterministic scenarios are synthetic,
not a replay claimed to be captured public evidence. Both markets exercise the
30-source-second opt-in acceptance trigger, later-event partial execution,
synthetic fees/slippage, cash/position updates and the existing PaperStore.
A 0.04-unit fill at 99.0099 incurs fee 0.003960396 and slippage cost 0.000396
in its respective account currency. Stocks cash becomes 996.035643604 USD;
Futures cash becomes 999.996039604 USDT with 3.960396 USDT position collateral.
The actual synthetic JSONL CLI replay completed in 1.419 seconds with 49,152 bytes
of SQLite state; these measurements are separate from the public online window.

Tests also cover cancellation, idempotent duplicate confirmations, same-configuration
restart in a separate process, changed-policy restore rejection, UNKNOWN query-only
orders, independent market disconnects, stale rules/quotes, closed/unknown sessions,
no-touch limits, public provenance forgery and serialized/environment attempts to
enable real network mutations. Real network mutations and Stocks submit/cancel
remain blocked by their unchanged adapter boundaries.

Targeted command:

```console
python -m pytest tests/trading/test_simulation.py tests/trading/test_realtime.py tests/trading/test_rule_observations.py tests/trading/test_paper_control.py -q
```

Result: **125 passed in 22.03 seconds**.

## Deferred verification

TODO: Validate long-duration reconnect/latency/storage behavior and the scoped-rule
snapshot resource profile in a separately authorized bounded campaign. Online
order/fill evidence remains NOT_VERIFIED; do not infer it from offline fills.

TODO: Verify real-account permissions, account-specific Futures brackets, Stocks
payment eligibility, settlement and custody only under separate read-only account
authorization. Paper assumptions cannot satisfy those checks or authorize real
execution. No real-account validation or live-money admission is granted here.
