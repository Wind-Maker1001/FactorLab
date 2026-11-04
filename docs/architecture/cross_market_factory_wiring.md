# P2-03 Cross-Market factory candidate on Baseline B2

Baseline B2 is `246a0722a84a5038da40f13c787b1134ae23cf7b`, tree
`baf45210cb1e6ce1db92fe5e5669a5aef230324a`. Only `research.make_model`
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

## Positional compatibility and independent acceptance

Baseline B calls `FactorScoreModel(weights)` with one positional argument.
The B2 factory supports an explicit positional channel. FactorScore uses
`LegacyModelRequest('factor_score', 'legacy-v1', 'cross_market_fixed_v1', {},
positional_arguments=(weights,))`. Normalization remains omitted. The other three
request branches retain their existing arguments and module-level bindings.

Both original Spy tests remain ordinary tests with unchanged behavioral assertions.
The frozen direct-caller Oracle is unchanged. Only migration-specific request
channel assumptions are updated. The public factory and models are unchanged
from B2. Earlier P2-03 commits and blocked receipts remain preserved.

Acceptance uses FACTORLAB_MIGRATION_EQUIVALENCE_V2.0: independent controls, a sealed
external native-boundary observer, preregistered real source locations, exact
artifacts, full Current/Legacy/V2 on both Python versions, and wheel/sdist installs.
Actual results and identities are recorded in the resumed task's deliverables.

Machine receipts and raw logs live in the task-local ignored `local-p203/evidence`
directory. Resumed B2 evidence lives separately in `local-p203-b2`. Both bind the
actual tested candidate SHA and retain real failures; no old result is replaced.
CN replay uses the frozen synthetic adapter and US sessions; US replay uses the
fixed frozen session calendar. Neither certifies real market data or calendars.

PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED / NO LIVE MONEY
