# Incremental ledger recovery

## Current operating boundary

The account event writer, control binding and capital-result reducer are available for isolated validation. Live service constructors do not enable these bindings. Use the existing planned handover procedure for executable recovery; a received incremental prefix alone does not authorize an unplanned takeover.

Production activation is disabled. Automatic fencing, takeover/publication and production dual-market replication wiring are deferred beyond this foundation delivery. Future activation requires complete producer/domain inventory, account ownership enforcement, compatible immutable strategy assets and proven publication/fencing conditions.

A portfolio carrying replay identity rejects legacy order creation without its account writer and always rejects `run_once`. Optional atomic `create(event_writer=...)` admits original plans/reservations but does not submit them. Opening that database directly does not bypass execution quarantine. Optional capital, risk and adapter budget preparation do not authorize execution.

## Admission and restart

An account has one stream/generation and one admission journal shared by all declared critical domains. Retain the existing account ownership lock across proposal, offsite confirmation and all domain applications. Do not dispatch orders or process another command while the account is partially applied.

- A transport failure leaves the original proposal pending. Retry `AccountEventWriter.resume()` with the same journal and reducers; do not replace the event or mint a replacement economic identity.
- An offsite ACK lost in transit is an ambiguous response, not a rejected economic event. A queued capital command stays pending until the owning runtime can finish the original result.
- A reducer failure retains the account proposal. Restart recovery finishes domains that have not committed; domains already at that batch skip duplicate application.
- Stable-ID retries must preserve original event content. Conflicting content and unknown domain targets are errors, not reasons to skip a batch.
- Mailbox task producers and account-owner consumers must share that same account sequence. A task ACK establishes durable queue acceptance, not execution completion. Preserve original receipt times, details and order when retrying or replaying.
- Capital command acceptance, economic postings and command completion are distinct events. If completion ACK is lost, resume the pending completion before executing the command again; retain the original result rather than recomputing a report or fabricating a rejection.
- Writer-backed policy/configuration calls run outside a surrounding SQLite write transaction, while retaining account ownership. Preserve stable control request IDs across retries. Initial allocations and flat ownership transfers use their original economic identities; replay preserves performance origins and predecessor/recipient historical attribution.
- `create(event_writer=...)` prepares a full original plan using a rolled-back transaction; policy claims, original inventory attribution, risk metadata/history and all child reservations apply as one portfolio event after confirmation. A request retry must preserve its original rebalance inputs.
- `admit_order_attempt` persists the original pre-POST marker without submitting an order. Pass the adapter's prepared dispatch through `prepare_dispatch` to include cumulative POST/exit-budget consumption (`demo_runtime`) or original intent/rate reservation (`alpaca`) in the same account event. Register the corresponding fixed reducer alongside `portfolio`. A false result, including after resuming a lost ACK, requires original-ID reconciliation rather than resubmission. A marker without dispatch is recovery bookkeeping only, never budget or execution authorization.
- Register `reduce_portfolio_event` for the portfolio database when replaying both capital and risk-control events. Do not register the same database as separate capital and risk domains.

## Snapshot connection

Capture the common account position while all financial writers and the admission journal are frozen at the same boundary. Snapshot metadata must bind the stream/generation, sequence/digest and complete fixed domain-to-database inventory. Capture this metadata before releasing the freeze, not during later online copying.

A copied domain can still carry its older initialization baseline. In isolated restore staging, `DomainReplay(..., baseline=position, restore_snapshot=True)` accepts the newer snapshot baseline only when the database already records that exact highest applied position and the same lineage. It cannot manufacture replication coverage for a database that lacks it.

`AccountReplay.catch_up(receiver)` verifies the snapshot connection digest against retained offsite history, pins one receiving cut and replays through that cut. New batches arriving during reduction belong to a subsequent catch-up. Completion means all declared domains reach that cut; it does not establish source isolation, official reconciliation, complete producer coverage or absence of an asynchronous tail.

Do not delete the receiving history containing a snapshot connection point while that snapshot remains eligible for restore. Generation rotation and safe retention are not yet implemented.

## Isolation before publication

Before any execution publication, independently establish all of the following:

1. The exact source executor is fenced and cannot restart with usable write authority.
2. Concurrent takeover attempts cannot publish different effective owners.
3. Every critical internal producer used offsite admission before local effect, or an authoritative final source state is available.
4. All domain applications reach the same verified cut, immutable strategy/model assets match their original identities, and official GET reconciliation closes orders, fills, cash and inventory under original ownership.

Deallocation without durable restart control is insufficient. Unreachable SSH is not isolation. Never infer missing capital movements from balances or rerun strategy inference to replace a missing historical decision.

## Local fault tests

From the repository worktree, using Git Bash:

```sh
PYTHONPATH="$PWD/src" python -m pytest \
  tests/integrations/test_ledger_replication.py \
  tests/integrations/test_ledger_replay.py \
  tests/integrations/test_ledger_control_admission.py \
  tests/trading/test_dynamic_capital.py \
  tests/integrations/test_binance_dynamic_capital.py \
  tests/integrations/test_alpaca_dynamic_capital.py \
  tests/integrations/test_alpaca_pending_capital.py \
  tests/integrations/test_demo_strategy_supervisor.py \
  tests/integrations/test_disaster_recovery.py -q
```

These tests use isolated SQLite state and simulated venue fixtures. Architecture, resource and official simulation drill measurements must be recorded separately in `evidence.json`; local test success is not a substitute for those measurements.
