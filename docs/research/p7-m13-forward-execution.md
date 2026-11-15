# P7-M13 — Binance Grid forward shadow observation

**NO LIVE MONEY.** This entry point uses the existing Demo Grid Preview and its
GET-only session. It never grants write authority or calls `execute`. All Preview
journals and feed cursors belong to the selected research output directory; they
are not the operator's execution journals.

## Observation method

The existing `GridConsumer` decides whether a closed bar is live. History and
replayed source events do not create new observations. An optional observer freezes
the strategy's actual active LIMIT reservations without changing price, quantity,
Grid configuration, risk conditions, or inventory. Stable deployment/client IDs
identify proposals. The complete deployment binding, configuration hash and source
version accompany each frozen proposal.

`proposed_at` is the durable local start of the strategy decision;
`captured_at` bounds its completion. `market_at` is the triggering bar's exclusive
close boundary, not a fabricated order timestamp. A durable `begin` record precedes
the Grid source commit. After an interrupted capture, recovery uses the existing
committed Grid source, completes its pending bar if necessary, and records the
original identities before Preview withdraws local reservations. A source that
never committed as live cannot be promoted by later historical replay. Research
capture does not write confirmations into the Grid execution journal.

The feed exposes bid, ask, quantities (when returned), exchange time and local
receipt/validation time from **one bookTicker response**. There is no additional
depth request and no asynchronous bid/ask reconstruction. Top quantities only
represent the visible best level; they are not full depth or a queue-position
estimate. Records include whether that opposite best-level quantity covers the
proposal quantity, signed LIMIT-to-bid/ask distances `(limit / quote - 1) * 10000`,
absolute spread, age, UTC hour and weekday. Market trading status is a separately
timestamped exchangeInfo observation, not an assertion of synchronous book status.

Books older than five seconds, future books and nonincreasing exchange timestamps
cannot update evidence. Feed-level validation can reject a whole batch before its
callback. A book fetched before proposal generation is only a baseline even if
its callback happens later. Forward evidence requires both book event time and
local receive time strictly after `captured_at`. Historical OHLC is never used to
infer a forward touch.

- **Not touched:** no sampled midpoint reaches the original limit in the side's
  execution direction.
- **Price touched:** sampled midpoint <= buy LIMIT or >= sell LIMIT. This is a
  price-path observation, not execution. First observed touch is retained.
- **Potential book condition:** ask <= buy LIMIT or bid >= sell LIMIT. Visible
  opposite quantity coverage is reported separately. This is not a fill.
- **Confirmed exchange fill:** always zero here. There is no exchange order.

Waiting time is from the persisted decision start to first observed touch.
Maximum deviation is the largest sampled absolute midpoint distance from LIMIT,
in bps. These are discrete-sampling statistics, not intrainterval extrema.
Observations end at the original absolute window deadline, strategy invalidation,
data interruption or bounded run end. Untouched terminal observations are right
censored, not proof that price never touched between samples. A restart records
an unobserved process gap and restores first touch, identity and original deadline;
it does not interpolate a price path or renew the observation window. Preview's
existing fresh-bar restart behavior may invalidate old reservations.

## Storage and limits

`events.csv` is a small append-only event stream; its single CSV field contains a
compact event object, including proposal batches and only necessary sampled book
facts. It is the sole observation recovery source, not a second JSON identity
index. Each append is flushed and synced before its callback returns. Malformed
or truncated records fail parsing rather than silently creating a replacement
history. The file is capped at 5 MiB by default, at most 10 MiB; 16 KiB is reserved
for censoring records. Default maximum is 20 proposals, hard maximum 100.

Default execution is 180 seconds, five-second samples, 180-second per-proposal
windows; hard maximum duration/window is one hour and minimum sample spacing is
one second. Sampling is the existing supervisor's inter-poll delay: reconciliation
and GET latency add to the observed spacing, and an in-flight poll completes before
the duration stop takes effect. Existing Preview SQLite journals and Feed cursor storage are reused
for strategy recovery, not replaced by a new database or background service.
They are runtime state, not published evidence. Do not run concurrent owners on
one output directory. To resume, keep the output directory and identical source
version, deployment and evidence limits. A completed run's terminal proposals
remain terminal; use a new directory for an independent experiment.

## Reproduce

From the repository root, with the existing read-only Demo credential/runtime
configuration available in Windows Credential Manager:

```bash
PYTHONPATH=src python -m factorlab.integrations.binance_futures_trading.forward_observation \
  --output /path/to/research-run --version "$(git rev-parse HEAD)" \
  --seconds 180 --sample-seconds 5 --window-seconds 180 \
  --max-proposals 20 --max-bytes 5242880
```

`--config` accepts an existing GridConfig JSON, using exactly the existing Grid
factory and immutable deployment behavior. Omitting it uses the unchanged Demo
Grid factory defaults. There is no execute option or new authorization flow.

```bash
PYTHONPATH=src python -m pytest -q \
  tests/integrations/test_forward_observation.py \
  tests/integrations/test_demo_market_feed.py \
  tests/integrations/test_demo_grid_supervisor.py \
  tests/integrations/test_binance_demo_grid_bridge.py
```

## Evidence

See `docs/validation/p7-m13/` for engineering results and the bounded real-run
result. Engineering fixtures are synthetic and are not real-market samples.
A successful public GET is not by itself a prospective strategy proposal.
`real_observation=NOT_RUN` records unavailable usable observation conditions;
a RUN with zero proposals remains a zero-proposal result. Neither category
establishes real exchange execution.
