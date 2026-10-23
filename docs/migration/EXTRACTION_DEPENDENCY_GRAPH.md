# Extraction dependency graph

```mermaid
flowchart LR
  CLI --> config
  CLI --> admission
  CLI --> data
  data --> DatasetBuilder
  DatasetBuilder --> factors14
  DatasetBuilder --> labels
  DatasetBuilder --> universe
  DatasetBuilder --> WalkForward
  WalkForward --> fourModels
  fourModels --> metrics
  metrics --> reports
  fourModels --> backtests
  forward --> sourceIdentity
  forward --> admission
  exploratory --> factors14
  exploratory --> metrics
  minuteTests --> yhh30m
  data -. optional .-> AKShare
  data -. optional .-> BaoStock
```

Production: NumPy, pandas, SciPy, sklearn, LightGBM, XGBoost, PyArrow,
Matplotlib, PyYAML and tzdata. Testing/build: pytest, coverage, setuptools,
wheel, build; black/flake8 for development. Suppliers: AKShare/BaoStock;
PDF/workbook parsing: pypdf/openpyxl; HTTP libraries are provider dependencies.
GUI, brokerage SDKs, Qlib and Backtrader are not part of the extracted tree.

Actual AST direct imports and per-module internal transitive closures for all
171 baseline production modules (162 research plus9 adapter) and175 candidate
modules are in evidence/static-import-dependency-audit.json. Baseline dynamic
imports target requests/urllib3 exception modules,AKShare,LightGBM,XGBoost.
Candidate also has internal FactorLab imports and the synthetic runner's
nonliteral namespace helper (defaults to factorlab, no CLI namespace option).
These nonliteral sites are explicit; static analysis alone does not prove
arbitrary dynamic targets. Separate isolated175-module import and offline
synthetic executions establish the tested default runtime independence.

## Physical and identity dependencies requiring migration

* config/loader.py:31-41 discovers an ancestor containing qbot/ and README.
* forward/integrity.py:107-162 binds qbot/research plus old requirements.
* diagnostics/model_readiness/source_identity.py reads committed Git blobs
  under qbot/research and requires an exact repository root.
* data/csmar/fingerprint.py:36-81 follows AST imports and config bytes from a
  root inferred from the old source layout.
* exploratory runners bind source paths and software identities.
* model_readiness and market_structure intentionally use namespace packages;
  wheel discovery must include directories without __init__.py.
* Historical reports, C-01 preservation and frozen delivery tests require
  old paths, commits and evidence. They are not authorization for new bytes.

## Independent security controls

market-admission.yml pins a separate trust anchor; research_checks.py verifies
its checker before loading it. C-01 has frozen legacy/source/reviewed commits.
The original baseline Git control objects/pins remain unmodified. Candidate
archive exports of three controllers have EOL conversion; they are not the same
raw trusted bytes. Both identities are now separately bound. The candidate
cannot approve its own control migration. Independent review remains a release
blocker; see GIT_OBJECT_VS_ARCHIVE_BYTE_IDENTITY.md.
