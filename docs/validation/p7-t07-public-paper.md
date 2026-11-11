# P7-T07 public-market paper acceptance

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED

Baseline: `4497cb134acfddaa4bcd8aebd7d236ae13ab4b80` (PR #110).

## Separate outcomes

| Observation | Futures NVDAUSDT | Stocks NVDA |
| --- | --- | --- |
| Offline deterministic closure | PASS | PASS |
| Online public market acquisition | PASS | PASS |
| Online real-quote-driven paper order | PASS (P7-T07R window below) | NO_TRADE |
| Online simulated execution | PASS (P7-T07R window below) | NO_TRADE |
| Account/position persistence reconciliation | PASS | PASS |

The engineering acceptance marker remains withheld: Stocks lacks official rules
and session evidence. Futures passed independently in the follow-up window; the
original window below remains a separate, unsuccessful online observation.

## P7-T07R bounded follow-up

One new window used the existing CLI with public data, paper simulation,
non-predictive acceptance signal, rule refresh, 60 seconds and at most two
reconnects per market. Fresh stores were used and all `BINANCE_*` environment
variables were removed. There was no account IO or live order transport.
The runtime recorded UTC 2026-11-11T19:08:12.355950Z–19:09:13.412663Z;
these measured timestamps are retained rather than relabeled to the task date.

| Observation | Futures | Stocks |
| --- | --- | --- |
| Public messages / accepted events | 260 / 260 (200 trades, 60 marks) | 626 / 626 quotes |
| Connections / reconnects | 1 / 0 | 1 / 0 |
| Observation rejections / errors | 0 / 0 | 0 / 0 |
| Official rules | VERIFIED, VALID | UNKNOWN; missing API key, ValueError |
| Official session | VERIFIED current TRADING status; schedule UNKNOWN | UNKNOWN; no calendar event |
| Strategy enabled at connection | yes | no, RULE_REFRESH_FAILED |
| Signals / orders / fills | 1 / 1 / 1 | 0 / 0 / 0 |
| Online paper order | PASS | NO_TRADE |
| Online simulated fill | PASS | NO_TRADE |
| Independent-process persistence reconciliation | PASS, including FILLED identity | PASS, empty ledger |

The Futures acceptance signal was `DETERMINISTIC_PAPER_ACCEPTANCE_NO_PREDICTIVE_CLAIM`.
The durable journal records submission at 19:08:43.564144Z and a later fill at
19:08:43.957299Z for `rt8145bf9e786bb2c4524a32b08fe5c3`, quantity 0.1,
price 176.277626000 USDT. The existing later-source-time and participation-budget
matching conditions remained unchanged. Risk admission was APPROVED with a
five-second quote limit. Synthetic fee was 0.0176277626000 USDT and slippage cost
0.0017626000 USDT. Final total cash was 999.9823722374000 USDT, with
17.6277626000 USDT collateral reserved and a 0.1-unit position. Stocks retained
1000 USD, no reservation, no position, no order and no fill. There were no order
or fill rejections; Stocks was blocked before signal generation by missing rules
and session evidence, not by a fabricated account-permission decision.

### Latency attribution

| Seconds, min–max (mean) | Futures | Stocks |
| --- | --- | --- |
| E − source | 0–0.151 (0.075915) | not instrumented |
| Application recv − E | 0.150437–0.398155 (0.167968) | not instrumented |
| Application recv − source | 0.152348–0.548155 (0.243883) | 0.640122–3.222890 (1.710059) |
| Normalizer start − recv | 0.000018–0.0000634 (0.00002795) | 0.0000141–0.0002998 (0.00002360) |
| Synchronous event handling | 0.008583–0.040574 (0.011647) | 0.005524–0.042840 (0.006754) |
| PaperStore BEGIN-through-COMMIT duration | 0.007455–0.039166 (0.010197) | 0.005245–0.042295 (0.006374) |

The E-minus-source aggregate includes 60 marks whose source is E, contributing
zero; trades use T. Its 19.738-second sum over 200 trades gives mean E−T
0.098690 seconds. All 260 Futures receive-minus-source values were in
(0.1, 1] seconds; all handler and transaction durations were below 0.1 seconds.
There were no rejected events to populate a rejected-event latency distribution.
Diagnostics retain bounded count/sum/min/max and six histogram buckets, plus
last transaction UTC start/end; they do not retain a market-data archive.
The two rejected-trade tests separately exercise source lag and emission/receive
lag while keeping the five-second rejection unchanged. The diagnostic metric
originally named `emitted_minus_trade` during this run is named
`emitted_minus_source` in the final code to include marks accurately.

A single unsigned Futures server-time probe bracketed local-minus-server offset
between −0.328014 and +0.154703 seconds (round trip 0.482659 seconds). It does
not support a multi-second local clock error in this window. `received` is the
application `recv()` return, not wire arrival: its difference from E combines
network/proxy transport and any WebSocket queue residence. Those components
cannot be individually attributed from this measurement.

**The cause of the original 41 rejections remains unproven.** The earlier run
retained full-universe exchangeInfo inside account snapshots, which are copied,
serialized and loaded synchronously. The existing target-symbol reduction now
covers that same snapshot path. The follow-up did not reproduce stale rejection
or multi-second synchronous blocking. The old run has no E/T/receive/transaction
breakdown to prove that copying alone caused all 41 rejections. No further
scheduling change or threshold relaxation is justified by this result.

### Stocks read-only contract

The pinned official SDK's
[`market_data_api.py`](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/rest_api/api/market_data_api.py)
classifies exchangeInfo as MARKET_DATA, requiring `X-MBX-APIKEY` but no signature.
The current authenticated reader is therefore retained. The account holder would
need only a key authorized for this read-only MARKET_DATA endpoint, not order,
withdrawal or transfer privileges. Configure `BINANCE_EQUITY_API_KEY` locally
through a protected process environment/secret store, restrict access and source
IP where supported, and keep it out of CLI arguments, logs, tracked files and
reports. No secret/signing key is needed for this endpoint; no private-account
verification is included.

The pinned
[`market_streams_api.py`](https://github.com/binance/binance-connector-python/blob/f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/stocks/src/binance_sdk_stocks/websocket_streams/streams/market_streams_api.py)
specifies one calendar message per market-phase transition, no periodic heartbeat
payload, and server polling every five seconds. No calendar message during the
short window is therefore a normal coverage limitation, not proof of an open
session. Rules and session remain UNKNOWN; quote data and local time confer no
session authority.

### Resources and recovery

Peak process working set was 104,751,104 bytes (99.90 MiB). Closed SQLite stores
were Futures 40,960 bytes and Stocks 16,384 bytes, total 57,344 bytes (0.05469 MiB).
The runner took 62.217480 seconds including rule acquisition and closure; each
observer was requested for 60 seconds, with approximately 61.0 seconds including
connection shutdown. No second online run or complete market-data mirror was made.

The old 160.68 MiB / 5.11 MiB measurements used the same working-set and combined
SQLite-size units but preceded scope reduction, had different market traffic,
and produced no orders. The new process also includes diagnostics and a clock
probe. These are observational comparisons, not a controlled benchmark or a
claimed percentage speedup.

A separate process restored both stores with the same policy. Account identity,
assets/collateral, positions, open orders and reconciliation matched the online
projections. Futures preserved its FILLED order, quantity, fee and confirmation
journal identity; Stocks preserved its empty ledger. Public rule/session authority
cleared on restart as intended. The overall two-market closure remains incomplete;
`P7_T07_PUBLIC_MARKET_PAPER_ENGINEERING_ACCEPTED` is not granted.

The final targeted command listed below passed **127 tests in 22.31 seconds**,
including added rejected-trade attribution cases. Existing stale observations
still reject at five seconds; no scheduler or admission-policy change was made.

## Original online window

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

TODO: Validate long-duration reconnect/latency/storage behavior only in a
separately authorized campaign. Stocks online order/fill verification remains
blocked by official rules and session evidence; Futures follow-up evidence must
not be generalized to Stocks or to long-duration operation.

TODO: Verify real-account permissions, account-specific Futures brackets, Stocks
payment eligibility, settlement and custody only under separate read-only account
authorization. Paper assumptions cannot satisfy those checks or authorize real
execution. No real-account validation or live-money admission is granted here.
