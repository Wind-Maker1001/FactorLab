# Python upgrade compatibility

Actual Windows x86-64 interpreters: baseline CPython 3.9.25, target CPython
3.14.0 (standard GIL build), compatibility CPython 3.13.9. New runtimes were
installed separately; the existing 3.9 environment was not upgraded.

| Dependency | Baseline | Target | Verification |
|---|---|---|---|
| NumPy | 1.26.4 | 2.3.4 | actual Windows wheel, 14-factor and dataset exact comparison |
| pandas | 2.2.3 | 2.3.3 | actual wheel, indexes/labels/dtypes exact in shared-input probe |
| SciPy | 1.13.1 | 1.16.3 | actual wheel, statistics and robustness tests |
| scikit-learn | 1.5.2 | 1.7.2 | actual wheel, Ridge fit/predict and train-only imputation |
| LightGBM | 4.5.0 | 4.6.0 | actual wheel, prediction exact in probe |
| XGBoost | 2.1.4 | 2.1.4 | actual universal/native wheel runs on 3.14 and 3.13 |
| PyArrow | 17.0.0 | 22.0.0 | actual cp314 Windows wheel, shared Parquet inputs/outputs |
| Matplotlib | 3.8.4 | 3.10.7 | actual wheel, four-model reports and figures generated |
| PyYAML | 6.0.2 | 6.0.3 | actual wheel, strict config contracts |

The initially tested XGBoost 3.1.1 changed predictions and both backtests beyond
the declared tolerance. Both boosters' saved configs had the same scalar
estimated intercept value, so this was not explained by a different intercept.
The version upgrade itself was isolated by retaining 2.1.4 under the new Python;
all XGBoost predictions then matched exactly. No math, hyperparameters or old
Golden were changed to compensate. The failed 3.1.1 run remains in evidence.

Python 3.14 Windows exposed different st_ctime values for path stat and handle
fstat of the same file. Creation time is now explicitly st_birthtime_ns on
Windows, with the old-runtime fallback. Device/inode, size, mtime, regular and
single-link checks, path safety and before/after content hashes remain active.
The CSMAR attack/parse suite passed 227 cases after this compatibility repair.

Optional AKShare 1.18.88/BaoStock 0.8.9 were installed separately for adapter
tests. AKShare's jsonpath dependency had no wheel; the all-wheel attempt is
preserved as a failed install. Pure Python source building was allowed for
suppliers while native core libraries still required wheels. No real data was
downloaded. Core-only clean wheel installation does not require suppliers.

Windows wheel/sdist, source and CLI evidence are recorded separately. Linux
execution and remote GitHub Actions remain NOT_RUN in this local task.

Sources: [pandas 2.3.3 Python 3.14 support](https://pandas.pydata.org/docs/whatsnew/v2.3.3.html),
[Python Windows timestamp contract](https://docs.python.org/3.14/library/os.html),
[XGBoost intercept parameter](https://xgboost.readthedocs.io/en/latest/parameter.html).
These references explain compatibility considerations; the actual local runs
and package locks establish what was installed and executed here.
