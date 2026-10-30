# P2-01: four-model construction factory

Design baseline: `9d513b0e0439f5b2952176a53b6c30c8bbbae124`.
Import the new API from `factorlab.models.factory`; package exports and all
existing callers remain unchanged. The implementation adds only this document,
`src/factorlab/models/factory.py`, and `tests/research/test_model_factory.py`.

`PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY`

## API and explicit identities

`ModelSpec` contains model ID, exact implementation version, parameter mapping,
ordered `FeatureRef` values, seed, threads, and profile ID. Every field is
explicit. `validate_spec(spec, catalog=DEFAULT_CATALOG)` returns an immutable
`ValidatedModelSpec` with canonical identity and all constructor defaults
expanded. `create_model(validated, catalog=DEFAULT_CATALOG)` returns a fresh
instance of the original class, without fitting, predicting, reading data,
writing artifacts, networking, or changing random state.

Validation uses an unfitted original constructor for range validation; creation
revalidates and then constructs the returned instance. These temporary objects
have no learned state. Manual assembly of `ValidatedModelSpec` cannot bypass
validation. No model wrapper or replacement implementation is introduced.

|Canonical ID|Only accepted alias|Version|Returned class|
|---|---|---|---|
|factor_score|FactorScore|legacy-v1|FactorScoreModel|
|ridge|Ridge|legacy-v1|RidgeResearchModel|
|lightgbm|LightGBM|legacy-v1|LightGBMResearchModel|
|xgboost|XGBoost|legacy-v1|XGBoostResearchModel|

Identity matching is exact; no case folding, dotted imports, entry points,
`latest`, configuration plugins, or user-controlled deserialization occurs.
`ModelRegistry.register(ModelRegistration(...))` accepts trusted Python
descriptors only, rejects duplicate `(model_id, version)` even for the same
descriptor, and refuses further registration after `freeze()`. The resulting
`ModelCatalogSnapshot` and descriptor defaults contain defensive read-only
copies. The built-in catalog explicitly registers exactly four descriptors.

```python
from factorlab.models.factory import (
    FeatureRef, ModelSpec, validate_spec, create_model,
)

features = tuple(FeatureRef.legacy(name) for name in ("MOM20", "REV1"))
ridge_spec = ModelSpec(
    model_id="ridge",
    implementation_version="legacy-v1",
    parameters={"alpha": 1.0, "fit_intercept": True,
                "max_iter": 10000, "tolerance": 0.0001},
    features=features,
    seed=42,
    threads=1,
    profile_id="legacy_csi500_v1",
)
validated = validate_spec(ridge_spec)
ridge = create_model(validated)  # RidgeResearchModel, is_fitted == False

fixed_spec = ModelSpec(
    model_id="factor_score",
    implementation_version="legacy-v1",
    parameters={"weights": {"MOM20": 1.0, "REV1": -1.0},
                "normalize_by_absolute_weight": False},
    features=features, seed=None, threads=1,
    profile_id="cross_market_fixed_v1",
)
fixed = create_model(validate_spec(fixed_spec))
assert tuple(fixed.weights) == ("MOM20", "REV1")

# Cross-market callers must explicitly preserve their existing 40-tree default.
tree_spec = ModelSpec(
    "xgboost", "legacy-v1", {"n_estimators": 40}, features,
    42, 1, "cross_market_fixed_v1",
)
tree = create_model(validate_spec(tree_spec))
assert tree.n_estimators == 40
```

`FeatureRef.legacy` supplies compatibility labels with version `legacy-v1` and
definition_hash `UNVERIFIED`. It asserts no authenticated factor definition.
Callers may supply explicit references; this constructor validates their shape,
not their financial provenance. Training binding, actual hashes, target identity,
market, frequency, price basis, lookback, and admission belong to later services.
`FeatureRequirement` leaves those unknown fields as `None` and states whether
a supervised target is required. These values do not authorize training.

## Parameters, profiles, and compatibility

Only these profiles are accepted: `legacy_csi500_v1`,
`cross_market_fixed_v1`, `research_v2`. They label caller intent and never
rewrite parameters or derive signs, seeds, candidate grids, or selection policy.
Omitted constructor parameters expand to the original class defaults for every
profile. A cross-market caller omitting `n_estimators` therefore gets the
original class default 200, and must supply 40 to preserve its entry-point
contract. A missing FactorScore weight mapping always fails.

Feature columns must be nonempty, unique, explicitly ordered references.
FactorScore weights must have exactly the same keys and are copied into feature
order. Negative and zero individual weights are allowed; an all-zero vector
fails through the original constructor. Input mappings and feature sequences
are copied, including nested weights. Separate calls never share mutable model
state.

`seed` is a nonnegative Python `int`, excluding `bool`; it is required for
supervised models. FactorScore requires `None`, with `seed_status` equal to
`NOT_APPLICABLE`. A caller preserving the supervised default must supply 42.
`threads` is a positive Python `int`, excluding `bool`. FactorScore and Ridge
accept only 1 and receive no fictitious `n_jobs`; trees receive the exact value.

Unknown parameter names, duplicate keys exposed by a mapping, unsupported
containers, numeric strings, booleans in numeric fields, NaN, infinity, and
out-of-range values fail explicitly. Boolean flags require actual `bool`.
`random_seed`, `n_jobs`, and `feature_names` are not parameter keys: the spec's
seed, threads, and feature references are their single source. A JSON parser
must reject duplicate keys before constructing a Python dict; a dict has already
lost such duplicate-key information.

|Model|Defaults and construction compatibility|Unchanged training behavior|
|---|---|---|
|FactorScore|Explicit fixed weights; normalization False; seed None; threads 1; original OrderedDict/feature order|No weight learning; NaN neutral zero; infinity rejected|
|Ridge|alpha 1; intercept True; seed 42 supplied by caller; max_iter 10000; tolerance 1e-4; threads 1|Train-only median imputer and StandardScaler; original sklearn Ridge solver; entirely missing training column rejected|
|LightGBM|200 estimators; lr .03; leaves 15; depth 5; feature/bag fractions .8; bag freq 1; alpha 0; lambda 1; min samples 20; seed 42 and threads 1 supplied by caller|Median imputer; four native seeds equal caller seed; deterministic/force_col_wise True; gain importance; no scaler|
|XGBoost|200 estimators; lr .03; depth 3; child weight 1; subsample/colsample .8; alpha 0; lambda 1; gamma 0; seed 42 and threads 1 supplied by caller|Median imputer; random_state and seed equal caller seed; hist method; gain importance; no scaler|

`effective_parameters(model)` reports the copied constructor fields from the
original object. Native estimator options remain owned by the unchanged fit
implementation; tests inspect them after isolated synthetic training. Numerical
repeatability is scoped to the same environment, not a cross-library or
cross-hardware guarantee. No tolerance is widened.

`InvalidModelSpec` subclasses the original `ModelInputError` for the new
configuration boundary. Unknown identity/version raises `UnknownModelVersion`,
also an `InvalidModelSpec`. Original constructor failures are retained as
`__cause__` when their validation is invoked. The stricter V2 configuration
checks intentionally reject numeric strings and other permissive legacy inputs.
The original direct constructor, fit/predict, dependency, and not-fitted
exception types are unchanged. LightGBM/XGBoost native packages are never
imported by factory construction; a missing package still produces its original
dependency error only at fit.

## P2-01C: original API construction adapter

P2-01 was merged by PR #4. P2-01C extends this module and document and adds
`tests/research/test_model_factory_legacy_adapter.py`; original model classes,
callers, strict validation/build functions, configuration and dependencies stay
unchanged. Import these additional interfaces from this module:

```python
from factorlab.models.factory import (
    LegacyModelRequest, create_legacy_model, effective_spec_from_legacy,
)

request = LegacyModelRequest(
    "xgboost", "legacy-v1", "cross_market_fixed_v1",
    {"feature_names": ["REV1", "MOM20"], "n_estimators": 40,
     "random_seed": 42, "n_jobs": 1},
)
model = create_legacy_model(request)  # exactly one original construction
observed = effective_spec_from_legacy(
    model, model_id="xgboost", implementation_version="legacy-v1",
    profile_id="cross_market_fixed_v1", request=request,
)
assert observed.parameters["n_estimators"] == 40
assert observed.explicit_arguments == tuple(request.constructor_arguments)
assert observed.strict_spec.parameters["n_estimators"] == 40
```

`LegacyModelRequest(model_id, implementation_version, profile_id,
constructor_arguments)` is frozen. Original `feature_names`, `random_seed`,
`n_jobs`, weights and other keywords stay in the argument mapping, preserving
omission versus explicit None. No value coercion, signature binding, default
expansion or parameter validation occurs before the actual constructor. Missing
and unknown keywords, malformed values and model-input errors propagate from
that one call with their original type, message, cause and construction phase.
The metadata boundary still rejects unknown model/version/profile identities.

The request snapshots dict/OrderedDict/UserDict/defaultdict weights, lists, tuples and NumPy
arrays without converting their types. Public argument access returns a fresh
defensive copy with a read-only outer mapping. Opaque custom Python objects and
one-shot iterators retain their identity and original protocols; callers own
their lifetime and must not mutate them during use. The adapter deliberately
does not execute arbitrary deepcopy/reduction hooks, consume iterators early,
or replace a custom numeric object's original float conversion. Requests are
trusted Python values, not a YAML/pickle/plugin loader or a universal object
freezer. Models never retain the request, spec, catalog or injection callback.

Object-dtype arrays and structured object fields recursively snapshot supported
mutable elements, including self-references; copying an array's storage alone
would leave request state exposed. Copy memo entries keep their original objects
alive for the duration of the snapshot, so temporary array views cannot reuse IDs.

`ModelRegistration` appends optional
`legacy_constructor: type[ResearchModel] | None = None` after its existing
fields. Old positional descriptor construction and strict `build` remain valid.
The four default registrations explicitly associate their original classes;
legacy creation requires that association and the exact `legacy-v1` identity.
Old custom catalogs without that association still serve the strict API, but
cannot silently opt into legacy execution. Registry freeze semantics are intact.

`create_legacy_model(request, *, constructor_type=None,
catalog=DEFAULT_CATALOG)` accepts the registered original class or its subclass
through the injection keyword. Future callers pass their **current module-level
class binding at each call**, preserving frozen Spy tests and isinstance checks.
Method-level mocks on such classes remain observable. Strings, unrelated classes
and arbitrary callable/Mock factories are rejected. There is no dynamic import,
global catalog mutation, cached Spy, fallback constructor or extra validation
construction. Constructors perform no fit/predict, data I/O, network, persistence
or global RNG changes; native tree imports remain deferred to original fit.

`effective_spec_from_legacy(model, *, model_id, implementation_version,
profile_id, catalog=DEFAULT_CATALOG, request=None)` returns a frozen
`LegacyEffectiveSpec`. It reads effective original fields without constructing
or training. `original_model_id` retains the supplied alias; `model_id` is
canonical. Ordered names, normalized parameters, seed and threads reflect the
actual object. Optional `request` is caller-supplied provenance: explicit keyword
names and omitted default names are recorded separately. Without it both are
None, because explicit/default origins cannot be recovered from model state.
The supplied request must be the one used to create the inspected model; this
metadata is not an authenticated model/request binding.

`status="STRICT_REPRESENTABLE"` includes a `strict_spec` when those effective
values fit ModelSpec. `status="LEGACY_ONLY_UNREPRESENTABLE"` includes a reason
and no strict spec for old successful inputs such as whitespace feature names
or unfitted supervised models whose feature_names=None is resolved at fit, or
Ridge seed int subclasses accepted by the old API but rejected by strict V2.
Original positive integers too large for strict finite-number checks also retain
legacy success and return this compatibility status. Inspection reuses the
strict numeric predicate without invoking a validation constructor.
This optional inspection never changes legacy construction success, invents
names/weights, or becomes a runtime gate. It does not call strict validation
and cannot trigger its temporary constructions.

Legacy constructors retain loose numeric-string conversions (FactorScore
weights and Ridge numeric fields), NumPy boolean flags where accepted, original
sequence handling and all original failures. Strict `validate_spec/create_model`
keep their intentional narrower acceptance domain and wrapped errors. Profiles
never infer 40 trees or FactorScore signs: future cross-market callers explicitly
carry 40 (or a supplied legal value), REV/VOL/AMIHU negative weights, normalization
False, supervised seed42/threads1 and FactorScore seedNone/threads1. Omitting a
tree count always retains original class default200. Runner-specific defaults,
candidate order, parameter receipts and call counts remain caller-owned.

Adapter tests use the unchanged direct classes and actual cross-market
make_model as oracles; they compare original errors, call-time Spy/Mock methods,
single construction, defensive snapshots, exact trained states/predictions,
train-only preprocessing, pickle/save-load bytes, lazy dependencies and zero
construction side effects. Actual acceptance evidence is external under
`D:/Code/FactorLab-archives/P2-01C-LEGACY-COMPAT-20261030`.

## Verification and evidence

The new tests use direct constructors as independent oracles. They compare full
initial state and pickle bytes, signature defaults, explicit parameters and
boundaries, alias/version errors, immutable snapshots, profile isolation, seeds,
threads, feature order, and exact rejection outcomes. Synthetic training compares
exact predictions, train-only imputer/scaler state, coefficients/importances,
native randomness options, serialized bytes, and trusted save/load round trips.
Fresh-process native-import denial and patched I/O, network, persistence,
fit/predict and RNG checks exercise the construction-only contract.

The external acceptance package is
`D:/Code/FactorLab-p2-01-evidence-20261028`. It records the final local commit,
actual interpreter and dependency copies, commands, exits, node inventories,
JUnit, hashes, baseline/current node preservation, independent V2 comparison,
installation checks, and protection checks for Phase 1. Evidence is generated
outside this checkout and does not alter old tests, validators, pins, or receipts.
Preliminary environment failures remain separate from final acceptance.

Reproduce on the committed candidate with separate output directories:

```powershell
python -B -m pytest tests/research/test_model_factory.py `
  tests/research/test_models_phase2.py tests/research/test_tree_models_phase3.py `
  tests/research/test_walk_forward_runner_phase2.py `
  tests/research/test_walk_forward_stage_diagnostics.py `
  --basetemp=D:/external-evidence/targeted-tmp
python -B tools/ci/integration_checks.py --suite current --output D:/external-evidence/py314
# Repeat the entire current suite with the independent Python 3.13 environment.
python -B tools/ci/integration_checks.py --suite legacy --output D:/external-evidence/full
python -B tools/ci/validation_v2.py --full D:/external-evidence/full/legacy `
  --current D:/external-evidence/py314/current `
  --additional-current D:/external-evidence/py313/current `
  --output D:/external-evidence/v2
```

The full unpartitioned legacy job retains its real failure exit and historical
50 FAIL / 136 ERROR observations; software V2 acceptance is a separate result.
The final evidence and delivery report, rather than older main or Phase 1 runs,
determine this candidate's acceptance. Build/install checks run on an external
copy so generated packaging files never expand this change's three-file scope.

## Next migration boundaries

After P2-01C is reviewed, accepted and merged, P2-02 can adapt the old
WalkForwardRunner's six construction sites to `create_legacy_model`,
preserving candidate order, validation metric, tie-breaking, fit/predict counts,
and its no-Train+Validation-refit policy. P2-03 can independently adapt the
cross-market make_model call through that same adapter, explicitly carrying its 40-tree defaults and fixed
sign-derived weights. Neither migration is implemented here.

Training services, target/input binding, V2 artifact envelopes and selection are
later steps. A fifth-model complete research path is not available from P2-01.
Fix a real shared baseline B only after the P2-01C merge, recording its actual
commit/tree, interpreters/dependencies, frozen synthetic inputs, original
comparators and hashes. P2-02 and P2-03 branch independently from B, retain their
original reports/receipts and receive separate equivalence and combination
acceptance. Neither migration is implemented by this adapter. Revert P2-01C to
remove the adapter while retaining strict P2-01; once consumers exist, revert
them first. Existing original-class model artifacts need no migration.
