# P8-S02: bounded automatic research and evidence-gated Champion selection

Custom strategy and parameter versions now share one explicit train/validation/test
protocol, capital/cost/execution contract and risk budget. Validation freezes one
version per strategy before a paired OOS comparison with the persisted Champion.
Promotion requires risk/capacity gates, minimum economic improvement, adjusted
paired uncertainty intervals and cooldown. If no challenger qualifies, the current
Champion identity and version remain in place.

Reuses custom strategy execution, portfolio evaluation, ExperimentTracker,
HAC/block bootstrap, Monte Carlo CVaR and the existing workflow lock. Includes
versioned JSON evidence, immutable tracker records, input/source snapshots,
replay configuration, a one-shot CLI and an optional Windows Task Scheduler entry.
The initial adapter supports unlevered daily long-only A-share research; S01
package identity integration remains a follow-up after its interface merges.

Validation: multi-strategy six-parameter synthetic experiment retained Cash;
positive point estimates had uncertain OOS advantage. Targeted tests cover
leakage, constraints, retention, promotion, reproducibility and crash reservation.
See docs/validation/p8-s02/REPORT.md for commands, measured evidence and limits.
No live activation or automatic merge.
