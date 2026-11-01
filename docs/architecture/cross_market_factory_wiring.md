# P2-03 Cross-Market factory candidate: BLOCKED

Baseline B is `ef91f3d26801dc660fbef8f3826bfa37732dff33`, tree
`8a785e635a96488352adb1aa825d52ed2f1ed601`. Only `research.make_model`
and its factory import change. Each branch passes a `LegacyModelRequest` with
`legacy-v1`, `cross_market_fixed_v1`, and the current module class binding as
`constructor_type`. There is one construction per call.

FactorScore retains the original dict comprehension: uppercase `REV`, `VOL`,
`AMIHU` prefixes have weight -1.0, every other name +1.0, original order and
duplicate-key behavior remain. Normalization stays omitted. Ridge passes only
`feature_names`. Trees pass `feature_names`, `n_estimators=estimators`, `n_jobs=1`.
The public default remains 40, including `run_models` and `run_research`.
The original `ContractError('unknown model')` branch remains before any factory
call. There is no coercion, learned weighting, or strict V2 validation.

## Acceptance blocker

Baseline B calls `FactorScoreModel(weights)` with one positional argument.
The frozen compatibility factory calls `constructor(**request.constructor_arguments)`.
The candidate therefore calls `FactorScoreModel(weights=weights)`. Ordinary
original models have equal bound values, initial/fitted state, predictions and
serialization, but a recording Spy observes different args/kwargs. A valid
`FactorScoreModel` subclass defining `__init__(self, weights, /)` succeeds under
Baseline B and raises TypeError under this candidate. This is an acceptance-domain
and exception-stage regression, even if the frozen numerical fixtures agree.

Two ordinary tests retain this failure without xfail or skip. This candidate is
not accepted and must not be merged. No factory or original model was changed.
A minimal prerequisite is a separate backward-compatible positional-argument
extension to `LegacyModelRequest` and `create_legacy_model`, preserving all existing
keyword requests, one construction and original errors. That change needs its own
tests, review and a newly frozen common baseline before P2-02/P2-03 resume. A local
wrapper, subclass, direct-call fallback, or altered Oracle would conceal the gap
and is not part of this candidate.

Machine receipts and raw logs live in the task-local ignored `local-p203/evidence`
directory. They bind the actual tested candidate SHA and preserve failing tests.
CN replay uses the frozen synthetic adapter and US sessions; US replay uses the
fixed frozen session calendar. Neither certifies real market data or calendars.

PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY
