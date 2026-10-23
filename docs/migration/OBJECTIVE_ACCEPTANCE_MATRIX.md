# Objective acceptance matrix

This matrix preserves the entire requested scope. PASS means an actual bounded
local result; PARTIAL/BLOCKED/NOT_RUN are not release approval. Final CI counts
and input bindings must be read from evidence/final-validation.json and the
ignored local-final/final-head-binding.json after finalization.

| Objective section / requirement | Current evidence | Disposition |
|---|---|---|
|1: fixed extraction worktree,branch,baseline|BASELINE_ENVIRONMENT.json and final Git status|Fixed0ec7af5870c54a0ee202fbceeb97b7d87c293411; read-only source|
|1: protect original worktree and pending PIT evidence|evidence/baseline/protected-worktree.json; protected-preservation.json|171 named files and HEAD/status rechecked; no source/data edits|
|2: independent FactorLab/src/CLI, no new services/trading SDK|pyproject.toml,175-module archive inventory,static-import-dependency-audit.json|Local independent package; no whole Legacy tree or broker/GUI dependency|
|3.1: actual direct/transitive/dynamic dependency audit|171 baseline/175 candidate AST import graphs,explicit dynamic sites; EXTRACTION_DEPENDENCY_GRAPH|Audited with declared AST limitations and separate actual offline execution|
|3.1: paths,Git identities,old trust/pins,pytest scope,licenses|BASELINE_AUDIT,SECURITY_ADMISSION_MIGRATION,LICENSE_AND_PUBLICATION_AUDIT|Old paths/control objects retained as provenance; new software identity non-authorizing|
|3.2: unchanged3.9.25 behavior baseline|Full baseline r2 JUnit4183PASS; BASELINE_TEST_RESULTS|PASS offline;2 network tests NOT_RUN; bad first round retained|
|4.1: qbot.research first, independent build/install|Candidate initial commit92fd378; legacy39 installed suite/probe|936 functional PASS;1 original-history assertion FAIL/BLOCKED; no original-source PYTHONPATH|
|4.2: factorlab namespace,configs,tests,CLI,manifests,resources|src/factorlab,450-file mapping,new identity,golden/reference preservation|Migrated; Python3.13/3.14 namespace packaging assertion checks real inclusion|
|4.2: preserve raw/Git/archive/history identities|GIT_OBJECT_VS_ARCHIVE_BYTE_IDENTITY and450 bindings|Original archive hashes retained;353 EOL-only exports separately bound to raw blobs|
|5: actual3.14 main/3.13 compatibility,controlled deps,wheels/ABI|PYTHON_UPGRADE_COMPATIBILITY,DEPENDENCY_LOCK_REPORT,locks,actual CI receipts|Windows execution; native core wheels; optional supplier pure-Python build recorded|
|5: no Golden regeneration/numerical drift hidden|CROSS_VERSION_NUMERICAL_VALIDATION; rejected XGB3.1.1 round|Final30 artifacts each version PASS;2.1.4 restored exact tree predictions; no speed claim|
|6.1:14 factors,Dataset,4 models,Walk-Forward,purge/embargo/leakage|Final runtime3915PASS each on3.14/3.13; full JUnit3980PASS/50FAIL/136ERROR; shared-input probe; public demo|Actual tests and demos;37 core ASTs equal imports-only normalization; full blockers not passed|
|6.2:IC/RankIC,paired,HAC,MBB,Holm,missing/constant/extreme/short samples|Retained metric/statistical/robustness/reference tests|Original math retained, frozen reference bytes verified including-O refusal probe|
|6.3:Fast/Execution,next-open,T+1,lots,status,limits,cash,fees,equity|Shared30-table comparison and retained backtest tests|Software equivalence; daily execution remains a stated approximation|
|6.4:39 discovery candidates,AST/provenance,32 market contracts/14 implemented,minutes/STATUS,Forward|Installed discovery/market demos plus final domain tests|Synthetic/software only;18 CONTRACT_ONLY features preserved|
|7:RAW/ADJUSTMENT/STATUS/PIT/SOURCE_BINDING/FORMAL refusals|Original gate plus additional unconditional migration guard; forged-allPASS negative|No qualification reduction; migrated independent financial trust root BLOCKED|
|7:old independent root,C-01,checker/replay/receipt/Forward/publish controls|Full suite retained;186 actual nonpassing traces reviewed;27 uncollected,135 setup-blocked and7 fixture-blocked assertion paths|BLOCKED; no self-signing,hash replacement or deletion of attacks; these counts overlap the full error/failure records|
|8.1:source pip,wheel,sdist,pip check,external import/CLI,no Legacy|PACKAGING_VALIDATION and clean core-only receipts|Actual Windows3.14 installations;175 modules/resource exact|
|8.2:public complete synthetic E2E,no suppliers|factorlab demo,175 offline imports,4 models/2 backtests,reports/manifests|PASS software-only; input replay refuses altered data; no real data acquisition|
|8.3:user data schema/existing adapters, no purchases/downloads|GUIDE/schema/provider/adapter tests|Interfaces retained; file existence does not authorize formal use|
|9:independent Windows/Linux3.14/3.13 CI,built artifacts,security scans|New workflow; actual local Windows jobs,packaging driver r7 and19 archive attacks on r6 artifacts|Local execution separated from remote; Linux/GitHub Actions NOT_RUN|
|10:README,Windows/Linux,quickstart,models,custom factor,schema,statistics,PIT/backtests,limits,contrib/security/upgrades|README,GUIDE,CONTRIBUTING,SECURITY|Documentation delivered; Linux instructions are not execution evidence|
|10:copyright/data/secret/publication review,no push/releases/PyPI|LICENSE/docs/LICENSE,publication report,archive scan,Git remote empty|Bounded archive inspection PASS; license/public-history review BLOCKED; no publication|
|11:all named migration reports,source,tests,CLI,demo,CI,docs|docs/migration inventory plus final evidence index|Delivered locally; raw logs retained privately and excluded from artifacts|
|12:minimum software candidate vs formal/public qualification|RELEASE_READINESS and final-validation.json|Core local candidate results distinct from full CI/financial/license release blockers|
|13:small local commits,read-only other worktrees,no destructive Git|Local commit log,remotes empty,protected snapshot|Local commits only; no push/merge/reset/clean/prune or other-worktree implementation|

The remote/Linux and independent-authorization gaps remain real unmet release
requirements. This matrix is an engineering completion audit, not a substitute
for those missing executions or independent reviews.

NO LIVE MONEY / PIT_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.
