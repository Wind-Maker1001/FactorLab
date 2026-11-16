# P7-M15 evidence

NO LIVE MONEY. `real/events.csv` retains raw market bookTicker objects and normalized events in the same bounded stream. `request` records are pre-dispatch reservations (including initialization and recovery), not successful HTTP responses or valid samples. `request_result` retains endpoint latency and error category without query parameters or private payloads. `experiment` fixes UTC start/deadline, source version and resource bounds before network access. Capture events retain complete deployment/configuration identity.

The CLI reserves 64 KiB of the 5 MiB research-data allowance for summaries and derived analysis. Preview SQLite state and feed cursors are local runtime state, not published evidence. The source/test files and test results are engineering artifacts rather than captured market data.

From this worktree, using the existing Python environment:

```bash
PYTHONPATH=src python -m pytest -q \
  tests/integrations/test_forward_observation.py \
  tests/integrations/test_demo_market_feed.py \
  tests/integrations/test_demo_grid_supervisor.py \
  tests/integrations/test_binance_demo_grid_bridge.py \
  --junitxml=docs/validation/p7-m15/offline-tests.xml

PYTHONPATH=src python -m factorlab.integrations.binance_futures_trading.forward_observation \
  --output docs/validation/p7-m15/real --version "$(git rev-parse HEAD)" \
  --seconds 3600 --sample-seconds 5 --window-seconds 3600 \
  --max-requests 1500 --max-proposals 20 --max-bytes 5242880

python docs/validation/p7-m15/analyze.py docs/validation/p7-m15/real
```

The source commit used for the actual capture is stored in its experiment event, rather than inferred from the later evidence commit. Omitted `--config` intentionally reuses the unchanged P7-M13 factory: deployment-specific initial range binds once to the initial real quote; its numeric prices are not transplanted from an earlier market window.

Sampling gaps and jumps are reported without interpolation. Feed validation may reject an entire poll before the book observer callback; such a batch is not counted as a captured valid book. A stopped or unavailable run is not replaced by replay. The analysis uses unique frozen proposals and valid subsequent book/proposal pairs, never GET totals, as sample counts.
