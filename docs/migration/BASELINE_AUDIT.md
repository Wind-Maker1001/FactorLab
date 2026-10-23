# Baseline audit

Baseline: `0ec7af5870c54a0ee202fbceeb97b7d87c293411`, branch
`codex/factorlab-extraction`, verified clean before execution.
No other worktree is an implementation target. Original quant-tv pending PIT
files and research source have a separate protected hash inventory.

Python 3.9.25 is reused read-only at `D:/Environments/factorlab-ci-py39`.
`pip check` passed. Actual package versions are in BASELINE_ENVIRONMENT.json.
Default pytest collection is only tests/research. This migration explicitly
includes model_readiness, market_structure, market_admission, minute_data and
evidence, without counting historical reports as new test success.

Baseline full-suite round 1 has setup errors because the selected basetemp
parent initially did not exist. Preserve its log and XML. Round 2 uses a
created same-drive parent. Neither round is a controlled performance benchmark.
Round 2 completed: 4183 passed, 2 network tests deselected, 88 warnings,
3557.59 seconds. Round 1 completed with 2825 passed and 1358 setup errors;
it is invalid for acceptance, not a successful baseline. Raw logs and JUnit
are preserved locally; BASELINE_TEST_RESULTS.md identifies both rounds.

162 research Python modules were parsed by static audit. No imports from Qbot
Legacy were found in production research. The minute-data tests/examples do
import qbot.engineering.yhh30m, a nine-module engineering adapter extracted
explicitly. Optional provider imports include AKShare, BaoStock and HTTP
exception libraries; no provider calls are authorized in this task.

NO LIVE MONEY. PIT_UNVERIFIED. FORMAL_ADMISSION_BLOCKED.
