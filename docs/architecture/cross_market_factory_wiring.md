# P2-03 Cross-Market factory migration on frozen B3

The baseline is da5ed287d328d5a65b1da94393695b75b372652a,
tree f3cde063b2853285c2293d029f2325b17c922c15. This candidate restores
the original four P2-03 migrations independently from B3, retaining the old
fd93fee5283854d5de4d521448879e76ed4c309e ancestry and its evidence.

FactorScore uses one positional TrustedLegacyReference(weights) in an otherwise
empty LegacyModelRequest for factor_score / legacy-v1 / cross_market_fixed_v1.
Normalization remains omitted. The original uppercase REV/VOL/AMIHU negative
signs, insertion order and duplicate dictionary-key rules remain unchanged.
The constructor receives the exact dictionary built inside make_model.

Ridge retains feature_names only. Both trees retain feature_names,
n_estimators=estimators, n_jobs=1. All public defaults remain 40 trees.
Unknown names raise the original ContractError('unknown model').
The B3 public factory, four native models and historical Oracle remain unchanged.

Independent acceptance uses the unchanged V2.0 comparator plus
FACTORLAB_MIGRATION_EQUIVALENCE_B3_V2.1 and actual live Python is observations.
Candidate-specific source locations are registered before candidate execution.
Full Current, Legacy, software V2, controls and isolated wheel/sdist runs are
required separately on Python 3.13.9 and 3.14.0. Old B2 results do not certify
this candidate. Legacy 50 FAIL / 136 ERROR retain their actual exit 1.

Frozen CN fixture replay uses synthetic data and US sessions and does not
certify real CN calendar or financial data. Financial and execution admission
remain blocked. This migration grants no merge authorization.

NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED
