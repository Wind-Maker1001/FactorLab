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

P2-02 can adapt the old WalkForwardRunner's construction calls to complete specs,
preserving candidate order, validation metric, tie-breaking, fit/predict counts,
and its no-Train+Validation-refit policy. P2-03 can independently adapt the
cross-market make_model call, explicitly carrying its 40-tree defaults and fixed
sign-derived weights. Neither migration is implemented here.

Training services, target/input binding, V2 artifact envelopes and selection are
later steps. A fifth-model complete research path is not available from P2-01.
Phase 1 integration must be arranged before caller migration is integrated and
validated at the final integration SHA. This candidate is local only: no push,
PR creation, merge, or financial admission is implied. Reverting these three
additions removes the factory without model-artifact migration.
