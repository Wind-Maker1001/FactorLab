# P2-02 WalkForwardRunner factory wiring

Acceptance status: recorded against the exact candidate in the task archive. Frozen Baseline B3 is
`da5ed287d328d5a65b1da94393695b75b372652a`, tree
`f3cde063b2853285c2293d029f2325b17c922c15`.

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
carries weights in positional_arguments and explicitly supplies the
normalize_by_absolute_weight keyword. The existing loops, selection,
fit, predict, save and load paths are untouched.

The original Baseline B and B2 call FactorScore with
`FactorScoreModel(self.factor_weights, normalize_by_absolute_weight=...)`.
B3 contains the separately accepted P2-01E explicit reference interface. This migration
uses `positional_arguments=(TrustedLegacyReference(self.factor_weights),)` and retains only the explicit
normalization keyword in constructor_arguments. The two original behavior probes
remain unchanged. The request-channel test additionally checks the actual tuple,
keyword order and empty positional tuples for the other models. Complete
acceptance uses FACTORLAB_MIGRATION_EQUIVALENCE_B3_V2.1 plus unchanged V2.0; passing these probes alone
does not establish migration acceptance. The shared factory and models are unchanged.

Independent evidence, commands, raw failures, frozen-control comparisons and
receipts are retained outside the checkout at
`D:/Code/FactorLab-archives/P202-B3-MIGRATION-20261104`. Original failure evidence
remains at `D:/Code/FactorLab-archives/P202-WALKFORWARD-FACTORY-20261101`.
The controls cover synthetic fixtures and software behavior only.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.

The real two-fold runner additionally checks live constructor-entry identity,
cross-fold sharing, positional-only compatibility, independent internal state,
native mutation, exception identity/cause and one-attempt behavior. A real
ordinary-request counterexample retains equal values/pickle but loses identity.
