# Offline research workflow

`NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED`

Use `tools/research/workflow.py` for a versioned research specification, local input binding, immutable ExperimentTracker evidence, Markdown report, and explicit local replay. No service or network-backed data provider is required.

## Run a complete local example

From the checkout, with the existing research dependencies installed:

```sh
python -I examples/research_workflow.py --output '<NEW_PRIVATE_ABSOLUTE_DIRECTORY>'
```

This generates synthetic observations for two securities, prepares the existing D01 input contract, executes a custom daily strategy twice and a six-strategy independent-account study, compares compatible runs, verifies identities, and replays both engines. `example-receipt.json` lists the outcomes. Each execution has its own `REPORT.md`, typed evaluation JSON, signals, NAV, trades, rejections, and artifact SHA256 receipts. Generated weekdays are not a certified exchange calendar; every run remains `SYNTHETIC`.

## Three separate kinds of input

1. **Data preparation configuration** belongs to the existing data-preparation tools. D01 pins its upstream manifest, snapshot, preparation config and quality report. The workflow consumes that contract, not a new data format.
2. **Portable research specification** binds `schema_version`, task, logical data ID and manifest SHA256, provenance category, parameters, windows, fees, fixed execution constraints, and research/admission boundary. Examples: [`p7-01-custom-daily.json`](../configs/research/p7-01-custom-daily.json) and [`p7-01-multisecurity.json`](../configs/research/p7-01-multisecurity.json). They reference the existing private D01 manifest; using them requires its exact original bytes.
3. **Runtime binding** supplies this machine's absolute local paths. It is saved as an artifact, but is not the portable data identity. Output/store paths are CLI arguments. Relocating identical inputs does not change their research identity.

A custom runtime JSON has exactly:

```json
{
  "research_root": "D:/private/d01-original",
  "source_root": "D:/private/csmar-prepared-original",
  "strategy_file": "D:/FactorLab/examples/custom_strategy/ma_strategy.py"
}
```

For `p6_r01`, omit `strategy_file`. Unknown or duplicate fields, unsupported task types, unsupported execution/admission settings and invalid types are rejected. Strategy constructor parameters are checked by the existing trusted-local strategy loader. Custom Python executes with the caller's privileges; only use trusted source.

Custom daily research preserves the original engine's `adj_close` requirement and constant adjustment-ratio restriction. It validates the D01 manifest chain first and then reads the genuine adjusted field from the hash-verified upstream snapshot. It never aliases raw prices to adjusted prices. An upstream snapshot that violates this existing engine restriction produces a precise failed receipt, not a replacement calculation. The synthetic example is the runnable reference for this route.

The `p6_r01` task uses D01/DataHub and the existing `account_batch` → six-strategy comparator. Declare each account's symbol, industry, year, research range, warmup and observed query endpoints explicitly. Query endpoints must retain every observed session in that range; gaps remain rejected. Accounts reset independently and summaries are descriptive, not a shared-capital portfolio. The provided two-account example is not the historical 54-account batch. The original frozen historical runner and its equality check remain separate and unchanged.

## Commands

```sh
python -I tools/research/workflow.py run --config '<RESEARCH_JSON>' --runtime '<RUNTIME_JSON>' --output '<NEW_OUTPUT>' --store '<TRACKER_ROOT>' --experiment-id study-a --run-id run-a
python -I tools/research/workflow.py inspect --store '<TRACKER_ROOT>' --run-id run-a
python -I tools/research/workflow.py compare --store '<TRACKER_ROOT>' --run-ids run-a run-b
python -I tools/research/workflow.py verify --store '<TRACKER_ROOT>' --run-id run-a
python -I tools/research/workflow.py replay --store '<TRACKER_ROOT>' --run-id run-a --runtime '<RUNTIME_JSON>' --output '<NEW_REPLAY_OUTPUT>' --new-run-id replay-a
```

`verify --runtime '<RELOCATED_RUNTIME_JSON>'` verifies byte-identical relocated inputs. Verification checks immutable tracker bindings, every saved artifact hash, portable configuration, source content, recorded commit/tree, Python/dependency versions, current manifest chain, custom source identity and actual consumed sample identity. Use the checkout commit recorded in the run; changing code is not replaying the same experiment. Paths, platform strings, output directories and elapsed times are not treated as numerical identities.

Replay is explicit and uses only local bindings; it does not download missing data. It compares every saved deterministic signal/ledger/table and typed evaluation result using `rtol=1e-10`, `atol=1e-12`. It reserves a new run ID and output directory; a mismatch is recorded as `FAILED / REPLAY_MISMATCH`, never a verified replay. Unavailable/null metrics must remain unavailable/null.

`inspect` and `compare` directly reuse ExperimentTracker, including its pre-existing records. `compare` requires successful compatible runs with matching data, sample, test windows, costs, evaluation contract and qualification. Historical records lacking those comparison identities remain rejected. Unified verification/replay supports only this explicitly versioned workflow format; existing tracker `reproduction()` and custom `replay_custom_run()` remain available for their historical formats.

## Lifecycle and immutability

The same Tracker stores `<run-id>-start` and `<run-id>` as immutable records. The existing Tracker status enum is unchanged; `validation.lifecycle` holds precise workflow state, reason, stage, timestamps and unfinished stages:

| Detailed state | Tracker state | Meaning |
|---|---|---|
| RUNNING | RUNNING | Started; absence of a terminal record means unfinished, not successful |
| DATA_UNAVAILABLE | NOT_RUN | Required local input missing before execution |
| INPUT_REJECTED | FAILED | Input/config binding or evidence identity rejected |
| EXECUTION_FAILED | FAILED | Existing engine or artifact production failed |
| REPORT_FAILED | FAILED | Report could not be produced |
| REPLAY_MISMATCH | FAILED | Replay execution completed but deterministic results differ |
| COMPLETED | SUCCEEDED | Engineering execution completed, including negative research outcomes |

CLI exit code is 0 for a completed operation and 2 for rejected/unsuccessful operations. Failure receipts preserve their actual stage and reason. Existing IDs or output directories are never overwritten. A hard interruption can leave only the start receipt: inspect `<run-id>-start` to diagnose an unfinished run. Experiment JSON is authoritative; the existing Tracker can rebuild its disposable index.

## Financial meaning

Reports distinguish net-cost price returns, unavailable total returns, descriptive statistics, simulated execution and formal admission. `TOTAL_RETURN_UNAVAILABLE / null` is neither zero nor a failed engineering run. In particular, bounded P6-E01 dividend entitlements are not added to NAV or reinterpreted as annual total return. Completing this workflow does not resolve PIT/HFQ evidence gaps or authorize live money.
