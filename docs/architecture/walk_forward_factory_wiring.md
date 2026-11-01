# P2-02 WalkForwardRunner factory wiring

Acceptance status: BLOCKED. Baseline B is
`ef91f3d26801dc660fbef8f3826bfa37732dff33`, tree
`8a785e635a96488352adb1aa825d52ed2f1ed601`.

The two tree preflight calls and the four training calls now each build a
`LegacyModelRequest` and call `create_legacy_model`. All requests use the original
model ID, implementation `legacy-v1`, and profile `legacy_csi500_v1`. Each call
passes the current module class binding through `constructor_type`. No constructor
binding is cached. The original model imports remain available to Spy subclasses
and the coefficient/importance dispatch.

The caller supplies the original explicit keyword subset. Ridge retains its
existing float(alpha) conversion and all six original keywords. Tree calls carry
feature_names, random_seed, n_jobs and the original candidate subset, without
expanding defaults or changing the JSON used for selection/reporting. FactorScore
carries weights and normalize_by_absolute_weight. The existing loops, selection,
fit, predict, save and load paths are untouched.

There is an unresolved interface conflict: Baseline B calls FactorScore with
`FactorScoreModel(self.factor_weights, normalize_by_absolute_weight=...)`.
`LegacyModelRequest` stores keyword arguments only, and the frozen factory calls
`constructor(**request.constructor_arguments)`. A Spy observes a different
positional/keyword shape, and an otherwise valid positional-only FactorScore
subclass cannot be called. The new tests retain the original contract without
xfail or relaxed expectations. This migration must remain a BLOCKED draft until
the conflict is resolved. The shared interface and Baseline B are not changed by
this PR. The frozen handoff requires any shared interface repair to be a separate
prerequisite PR followed by a new common baseline freeze.

Independent evidence, commands, raw failures, frozen-control comparisons and
receipts are retained outside the checkout at
`D:/Code/FactorLab-archives/P202-WALKFORWARD-FACTORY-20261101`.
The controls cover synthetic fixtures and software behavior only.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.
