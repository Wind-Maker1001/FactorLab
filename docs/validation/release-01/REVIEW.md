# Release patch review

Scope: baseline `bbda212299ccb1b6c2b4dcb021662397d19923d0` to deployed patch `ea9cbbdd7519e5400df097e5661b0d6c47b6c4da`, followed by the PR-only upgrade-boundary protection.

The patch review found an upgrade boundary: expanding historical consumption can exhaust an old authorization that still has exit commitments. The follow-up validates historical consumption plus reserved exits atomically during budget migration, including idempotent requests, and before continuous-window activation. Insufficient authorization blocks activation and requires an explicit budget update; reservations and dispatch history are not removed, and hard limits are not bypassed. No additional Azure migration or deployment is performed.

The preserved deployment snapshot has zero exit reservations and substantial budget headroom. It is not evidence that every possible legacy authorization can be upgraded without adjustment.

## Deferred baseline findings

These findings concern unchanged baseline code, not additions in the deployed patch. They remain separate from this archival fix; they are not evidence of an incident in the running accounts.

- FIXME: `src/factorlab/trading/portfolio_execution.py`, share-changing corporate actions: union is applied to a sorted list rather than combined sets, raising TypeError for affected active plans.
- FIXME: `src/factorlab/integrations/binance_futures_trading/demo_portfolio_consumer.py`, cancellation dispatch: an uncertain DELETE permanently consumes its identity even if authoritative recovery confirms the order remains live.
- FIXME: same consumer, zero-budget handling: zero available allocation rejects a liquidation task before reduce-only planning.
- FIXME: `src/factorlab/integrations/binance_futures_trading/execution.py`, shared margin: shared-account metadata overrides the active plan's aggregate margin ceiling.
- FIXME: `src/factorlab/integrations/alpaca/corporate_actions.py`, delayed receipts: chronology rejection can persist after settled post-action trading.
- FIXME: `src/factorlab/integrations/alpaca/cloud_execution.py`, quote cutoff: retrying with a newly advanced decision boundary can repeatedly exclude fresh quotes.
- FIXME: same executor, predecessor plans: preparation dereferences absent legacy signal checkpoints.
- FIXME: `tools/install_tradfi_observer.sh`, live install: execute is not restarted to adopt its new mailbox environment and sandbox.

These deferred findings do not authorize changes to running services. The PR's validation establishes its declared patch scope, not absence of all baseline defects.
