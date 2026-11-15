# P7-M13 evidence

NO LIVE MONEY. Engineering fixtures, real market observations and exchange fills
are separate evidence classes.

## Real prospective observation

`real/events.csv` is the raw bounded observation event stream;
`real/summary.json` is the emitted aggregate. The run used the unchanged existing
Demo Grid factory, with no custom price or condition adjustment.

- Observed on 2026-11-15 UTC, exchangeInfo reported `TRADING` at
  12:26:31.401486. This is a timestamped status fact, not evidence of underlying
  US equity exchange hours.
- Ten same-response book snapshots, market timestamps
  12:26:41.779–12:29:32.323 UTC; book age 0.974–3.363 seconds.
- One actual strategy BUY LIMIT proposal, `preview-g-00000002`, quantity 0.04,
  original LIMIT 159.87000; triggered by the 12:27 UTC closed one-minute bar.
- Seven later forward snapshots after the proposal baseline; bid 161.14000,
  ask 161.49000. No observed midpoint touch or opposite-best-level crossing.
- LIMIT distances: -78.8135 bps to bid, -100.3158 bps to ask. Maximum sampled
  absolute midpoint deviation from LIMIT: 90.3859 bps.
- The untouched proposal ended `RUN_END` at 12:29:33.350289 UTC, before its
  original 12:30:19.763402 observation deadline: one right-censored result.
- GET 206, POST 0, DELETE 0. Actual sample spacing was about 17–21 seconds,
  including serial account reconciliation and network latency; the configured
  five seconds is the supervisor's inter-poll delay, not a guaranteed quote rate.
- **Confirmed exchange fills: zero.** No exchange order exists for this proposal.

Run command from the repository root:

```bash
PYTHONPATH=src python -m factorlab.integrations.binance_futures_trading.forward_observation \
  --output docs/validation/p7-m13/real \
  --version 17b48ffed1ba8f8474f4e16f57a2ffe9b89ac0c8 \
  --seconds 180 --sample-seconds 5
```

The version in this initial run identifies its main baseline; the observer was
uncommitted implementation on that baseline. The delivered PR records that code
and subsequent engineering corrections. Reproduction should pass the actual
checked-out commit as `--version` and use a fresh output directory. Original
observations have not been relabeled as observations of a later revision.

Runtime Preview/account journals and cursors are local recovery state and are not
published. Only the observation stream and compact aggregate are research evidence.

## Engineering validation

The affected test command documented in the research report completed with
**90 passed in 451.30 seconds**. `offline-tests.xml` records individual cases.
The first run exposed a recovery decode defect and an invalid out-of-order
fixture (85 passed, 2 failed); both were corrected before this passing run.
Synthetic fixture touches must not be counted as real market samples or exchange
fills.
