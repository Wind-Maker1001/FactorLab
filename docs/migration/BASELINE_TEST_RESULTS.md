# Python 3.9 baseline execution

The fixed subject is `0ec7af5870c54a0ee202fbceeb97b7d87c293411` in the
user-created extraction worktree. CPython 3.9.25 and its dependency environment
were reused without upgrades. This report describes new execution, not an old
upstream PASS copied into this migration.

| Run | Passed | Failed/errors | Other | Result |
|---|---:|---:|---|---|
| Full baseline r1 |2825|1358 setup errors|2 network deselected|FAIL; invalid setup round retained|
| Full baseline r2 |4183|0|2 network deselected, 88 warnings|PASS, 3557.59 s|
| Independently installed stage-one qbot.research |936|1 historical-object assertion|88 warnings|PARTIAL, 314.84 s|

Full collection explicitly covers research, model_readiness, market_structure,
market_admission, minute_data and evidence. The default upstream pytest scope
was only tests/research. The two network cases were not executed: real data
downloads and external provider calls are outside this task.

Round 1 selected a basetemp whose parent did not initially exist. The directory
was created while the process was active, so its mixed outcomes cannot be used
as a clean baseline. Round 2 used a fresh same-drive directory and completed
all 4183 offline cases. Both logs/XML remain unchanged as separate rounds.

The first independent extraction uses its own installed qbot.research wheel,
with numeric dependencies available from the unchanged baseline environment;
it has no PYTHONPATH pointing to the Qbot checkout and no Legacy engine/GUI.
Its sole failure requires a historical upstream Git object not present in the
new independent repository. It remains a failure/blocker; 936 functional cases
and the separately executed installed numerical probe establish the narrower
engineering result. It is not described as an all-green stage-one suite.

Raw evidence: `evidence/baseline/full-suite-r2.{log,xml}`,
`evidence/baseline/full-suite.{log,xml}`, `evidence/legacy39-suite.{log,xml}`.
Machine-readable counts/hashes are in `evidence/test-summary.json`.
The shared-input four-model outputs are separate from all frozen Golden.

No controlled throughput comparison was performed. Concurrent validation load,
different scopes and coverage prevent using suite durations as a speedup claim.

NO LIVE MONEY / PIT_UNVERIFIED / FORMAL_ADMISSION_BLOCKED.
