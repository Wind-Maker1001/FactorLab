# Unified strategy management V2

This document defines the public boundary. `FACTORLAB_DECISION_V1` and
`factorlab.demo.evaluate/v1` retain their existing payloads, bindings and validation.
V2 is opt-in; passing a V2 decision to a V1 runtime is an error.

## Identity and authority

A `StrategyBinding` has five required string fields:

```python
market: str       # trading.contracts.Market.value
account_id: str   # opaque configured account identity, never a credential
instance_id: str  # durable instance identity, independent of package/version
allocation_id: str # stable execution-allocation identity, not an amount
strategy_id: str
```

`instance_id` is unique in a deployment database. Market, account and strategy
identity cannot change for a registered instance. A new binding requires a new
instance. Package versions are immutable and may be shared by multiple instances;
execution receipts, state and order ownership may not. Allocation identity is
immutable for an instance; its configured budget may change through the account
owner's allocation mechanism. Multiple instances may trade the same symbol.
Every receipt and order attribution carries instance and allocation identity;
symbol identity alone never establishes ownership. The execution adapter enforces
account policy on opposing exposures, reduce-only constraints and allocation-level
close attribution against the single account ledger. Shared-symbol registration
does not imply permission for internal netting or opposing strategy positions.

The market adapter resolves account identities and checks execution authority.
Neither a package nor a strategy decision grants authority or allocates funds.

## Strategy declaration

`factorlab.strategies.protocol` exports `StrategySpecV2` and `DecisionV2` in
addition to the unchanged V1 classes. The new protocol constant is
`FACTORLAB_DECISION_V2`.

```python
StrategySpecV2(
    strategy_id: str,
    version: str,
    market: str,
    account_id: str,
    symbols: list[str],
    decision_kind: str,
    data: dict,
    schedule: dict,
    state: dict,
    capital: dict,
    execution: dict,
)
```

The declaration describes strategy requirements. `instance_id` is supplied by the
host binding, not baked into reusable strategy source.

- `decision_kind`: `target_weight`, `target_weights`, `target_quantities`,
  `quantity_deltas`, or `orders`.
- `data`: `data_types: list[str]`, `history_limit: int`,
  `historical_observations: bool`. Types are `closed_bar`, `quote`, `order_event`,
  `funding_forecast`, `funding_settlement`, `volatility`.
- `schedule`: exactly one of
  `{"kind": "interval", "interval_seconds": int}` or
  `{"kind": "calendar", "calendar_id": str, "timezone": str, "event": str}`.
  A calendar is resolved by the host; the protocol does not implement calendars.
- `state`: `version: str`, `migration: "reset" | "preserve" | "migrate"`,
  `from_versions: list[str]`. `preserve` requires the same schema version;
  `migrate` requires a declared source version and the migration method below.
  `reset` explicitly starts with an empty state.
- `capital`: `currency: str`, `requested: Decimal`. This is a requirement, not an
  allocation; the adapter decides whether configured capital covers it.
- `execution`: `order_types: list[str]`, `time_in_force: list[str]`,
  `shorting: bool`, `fractional_quantities: bool`, `reduce_only: bool`.
  Adapters reject unsupported requirements before activation.

Empty symbol sets, duplicate symbols, undeclared capabilities, nonfinite Decimal
amounts and invalid scheduling requirements are rejected at the public boundary.
The adapter determines symbol tradability, quantity/price steps, leverage,
notional constraints and market-specific financial eligibility.

## Decision and serialization

```python
DecisionV2(
    kind: str,
    targets: dict[str, Decimal] = {},
    orders: tuple[OrderIntent, ...] = (),
    cancellations: tuple[dict, ...] = (),
    state: dict = {},
)
```

`kind` must match the declaration. Default containers above describe values, not
shared mutable Python defaults.

- `target_weight`: exactly one symbol in `targets`; value in `[0, 1]`.
- `target_weights`: weights for one or more declared symbols. Signed values
  require declared and adapter-approved shorting. Adapter capital/leverage
  constraints apply to the portfolio; this protocol does not assume net exposure
  is the risk budget.
- `target_quantities`: absolute desired signed position quantities.
- `quantity_deltas`: signed changes to current quantities. A delta is applied
  once per durable decision identity, never again on replay.
- An empty `targets` map means no position intent. Omitted symbols are unchanged;
  an explicit zero target flattens that symbol. Zero delta changes nothing.
- `orders`: uses existing `factorlab.trading.contracts.OrderIntent`. No new order
  state machine is introduced. Target modes cannot also emit direct orders.
- Cancellations are `{"symbol": str, "client_order_id": str}` and are available
  in `orders` mode only. The adapter verifies ownership by account and instance;
  cancellation IDs alone never authorize canceling another instance's order.
- All symbol references must belong to the declaration; order market must match
  the binding. Client order IDs and cancellation identities cannot be duplicated
  within a decision.
- `state` is a JSON object, not an account snapshot or trading ledger.

V2 payloads include `protocol: "FACTORLAB_DECISION_V2"`. Decimal values and
OrderIntent use the existing `trading.storage.dumps/decode` wire encoding;
Decimal is never converted to a float. Spec and decision payload helpers build
explicit nested maps, so new strategy dataclasses need not be added to the
trading-core type registry. V1 helpers retain their exact output shape.

## Runtime event

```python
{
    "protocol": "FACTORLAB_DECISION_V2",
    "binding": {"market": ..., "account_id": ..., "instance_id": ...,
                "allocation_id": ..., "strategy_id": ...},
    "event_id": str,
    "event_time": datetime,       # UTC, causal decision time
    "data_type": str,
    "symbols": {
        symbol: {
            "watermark": str,
            "observations": {
                data_type: {"observed_at": datetime, "effective_at": datetime,
                            "source": str, "payload": dict}
            },
            "history": {data_type: list[dict]},
        }
    },
    "account": dict,
    "allocation": dict,
}
```

Each history entry uses the same observation shape. Closed-bar payloads use the
existing bar field names; actual funding settlements use existing FundingEvent
fields. Forecast funding and settled funding are distinct data types, never a
single overwritten rate. Volatility payloads identify their estimator and
window. `observed_at` must not be later than decision time; future `effective_at`
is allowed for forecasts, not evidence of a realized settlement. A host supplies
only information causally available at event time.

An optional `context` data object carries adapter-specific projections such as
`context.native_v69`. It uses the same typed JSON boundary, participates in the
input identity, and cannot carry host objects, stores, callbacks or credentials.

The host supplies read-only account/allocation projections, with no controller,
store, transport, credentials or trading callback. An event can update a subset
of symbols; unchanged observations retain explicit watermarks. Durable decision
identity is `(instance_id, package_id, event_id)`; conflicting content under the
same identity is rejected, not silently reused.

## Strategy lifecycle

```python
class UnifiedStrategyV2(Protocol):
    def describe(self) -> StrategySpecV2: ...
    def decide(self, event: dict, state: dict) -> DecisionV2: ...
    def migrate_state(self, state: dict, from_version: str) -> dict: ...
```

`migrate_state` is required only for `migration="migrate"`. All three operations
run through the existing isolated loader/worker. A migration receipt binds the
instance, switch, source/target package, schema versions and input state. The
result is committed once and reused across recovery; switching away and back
creates a new switch and does not reuse an older source state.

## Packages

Public module: `factorlab.strategies.package`.

Existing package build/install/load behavior is extracted, not reimplemented.
Schema 1 identities remain unchanged. Schema 2 requires:

```python
schema_version = 2
interface = "FACTORLAB_DECISION_V2"
strategy_id: str
strategy_version: str
market: str
account_id: str
symbols: list[str]
requirements: dict  # data, schedule, state, capital, execution as above
execution_mode: str # decision_kind
```

Existing `code_commit`, `entrypoint`, `files`, `configuration`, `dependencies`,
`evidence`, `migration`, and `package_id` retain their meanings. Top-level
`migration` remains the position-draining policy (`wait_flat` or `reduce_only`),
not JSON state migration. A V2 package does not carry the old singular `symbol`
or Demo-only `environment`; its adapter validates the market/account binding.
Package ID covers the full manifest except `package_id` itself. In manifest JSON,
`requirements.capital.requested` is a decimal string; `manifest_spec` reconstructs
its Decimal value. JSON state remains ordinary JSON. Runtime spec/decision
payloads use the existing typed Decimal wire encoding.

Public functions retain `build_package(source, manifest, destination)`,
`install_package(archive, releases)`, `load_package(path)` and
`validate_manifest(manifest)`. Each accepts keyword-only `qualify=None` for a
market qualification callback after structural validation. Qualification never
changes the manifest or content identity.

The Binance `strategy_package` module retains old imports/signatures and applies
Binance qualification. Public loader code imports the public package module,
not Binance code. Legacy Demo qualification remains unchanged; qualification of
V2 dynamic symbols is supplied by the Binance symbol registry adapter.

## Deployment and execution adapters

Public module: `factorlab.strategies.deployment`, extending the existing store and
controller. Existing Binance imports remain compatibility exports.

```python
DeploymentStore(path)  # legacy-compatible default view
store.register_instance(binding: dict, *, fallback_package_id: str | None = None)
store.for_instance(instance_id: str) -> DeploymentStore  # caller closes the view
store.instances() -> list[dict]
store.register_package(manifest) -> None

# All methods below act only on the selected instance.
view.snapshot() -> dict
view.pending() -> dict | None
view.summary() -> dict
view.request(manifest, candidate, *, mode="hot", boot="unknown") -> int
view.rollback(reason, *, expected=None, expected_active=None)
view.record_order(client_id, package_id, event_id, *, reduce_only=False)
view.order(client_id) -> dict | None
view.commit_decision(package_id, event_id, input_identity, decision,
                     *, event, expected_checkpoint) -> dict
view.checkpoint() -> dict
view.deployment_id_for(package_id, prefix) -> str

DeploymentController(view, adapter, *, load_strategy=None).step() -> dict
InstanceDecisionRuntime(strategy, view, adapter, *, allocation).decide(event) -> dict
InstanceDecisionRuntime(strategy, view, adapter, *, allocation).recover() -> None
```

`register_instance` is idempotent only for identical binding and fallback.
New instances start with `active=stable=previous=None` until first activation;
only the legacy default starts with `legacy-grid`. A failure without a previous
or configured fallback package leaves the instance inactive/blocked, never
activates an unrelated legacy strategy.

`InstanceDecisionRuntime` is exported from `factorlab.strategies.decision_runtime`.
It persists a decision before dispatch, advances its checkpoint atomically, and
recovers unacknowledged receipts before accepting another event. A receipt contains
`binding`, `package_id`, `event_id`, `input_identity`, `event`, and `decision`.
`decision` is the versioned payload map, with Decimal targets and existing
OrderIntent objects reconstructed by the shared wire decoder. The decision key is
`(binding.instance_id, package_id, event_id)`; `input_identity` binds the complete
event, not just prices. The executor returns normally only after every child
intent is durably owned by the existing execution core. This acknowledges intent
acceptance, not order completion or fill confirmation. Uncertain and nonterminal
orders remain with that core and block deployment through reconciliation. If
child intent persistence is incomplete, the executor raises; recovery reuses the
same receipt and stable child IDs. An unfinished dispatch must recover before a switch is
requested. `load_strategy(package_id)` is required for nonlegacy deployment
controllers and returns an isolated `LoadedStrategy` for state migration.

### Native deployment adoption

`view.adopt_existing(manifest, checkpoint, watermark, evidence, *, verify)` adopts
an unchanged existing native runtime without a switch or position drain. It is
restricted to a pristine registered V2 instance. The required host verifier is
called as `verify(manifest, binding, checkpoint, watermark, evidence)` and raises
if native code, account journal, frozen checkpoint or watermark do not match.
Evidence is ordinary JSON and requires `journal_id` and a 64-character lowercase
`source_code_sha256`; adapters may add original plan/snapshot identities. The
verifier must validate these against host-owned records, not trust the strings.

Adoption and the complete request identity are committed atomically. Exact
redelivery returns current state without rewinding a checkpoint or rerunning
verification; changed requests are rejected. A later real version change still
requires the normal switch saga. The API does not create an account ledger,
change order IDs, cancel outstanding orders or allocate capital.

The adapter preserves the existing `boot_id`, `quiesce`, `reconcile`, `watermark`,
`cancel`, `reduce`, `prepare`, `activated` boundary. Every adapter is bound to a
single instance, and its reconciliation view includes only that instance's
orders and owned allocations. An unresolved account-wide inconsistency may
block execution; it does not reassign order ownership. Shared account funds,
net positions, reservations and risk stay with the existing account execution
owner. Quiescing an instance cannot pause the account or another instance.

A separate decision execution interface consumes the persisted receipt:

```python
class DecisionExecutionAdapter(Protocol):
    def qualify(self, spec, binding, allocation) -> None: ...
    def execute(self, receipt: dict) -> None: ...
```

The adapter uses the existing controller and journal to give a receipt's intents
stable IDs. `execute` must be restartable: a partially executed multi-symbol
decision resumes outstanding intents, not a second whole portfolio delta.
The public layer does not create additional Session/Gateway/account owners.

## Database compatibility

The original `deployment`, `switches`, `switch_events`, `strategy_orders`,
`decisions`, and `deployment_bindings` SQL layouts are preserved for the legacy
view because existing release code issues positional INSERTs and `id=1`
updates directly. Nonlegacy instance state uses instance-keyed companion tables
in the same database and the same controller/store code path; this is not a
second saga implementation. Shared packages retain content-addressed identity.

Migration records the legacy instance and schema version transactionally without
rewriting its state, orders, switch IDs or deployment bindings. New companion
keys and the pending-switch unique index are instance-scoped. Restarting a
migration cannot create duplicate default instances or discard history. Public
nonlegacy consumers use methods, not SQL against legacy tables.

## Champion routing

V2 promotion schema: `FACTORLAB_CHAMPION_PROMOTION_V2`.

```python
{
    "schema": "FACTORLAB_CHAMPION_PROMOTION_V2",
    "binding": StrategyBinding,
    "selection_id": str,
    "decision": "PROMOTE",
    "qualified": True,
    "strictly_better": True,
    "expected_active": str | None,
    "package_id": str,
    "evidence": dict,
}
```

`factorlab.strategies.promotion.route_promotion(store, promotion, manifest,
mode="hot", boot="unknown")` requests a switch and returns its ID; `mode` and
`boot` are keyword-only arguments. This router resolves exactly one instance and rejects binding, package,
evidence or active-baseline mismatches. Deduplication is instance-scoped. Market
adapters validate native selection evidence and execution eligibility before
creating this envelope. Existing V1 Binance selection adaptation stays intact.
