# MC-GRID-OPT-01 — static grid parameter research

The study evaluates complete-account outcomes rather than realized grid cycles alone. It does not define a single business utility function or a deployment risk budget. The frozen geometric comparator is unchanged from MC-05. Arithmetic, cash, fixed-first-close limit holdings and exactly matched endowed holdings make differences in trading exposure explicit.

## Reproduction

Use the repository's native CPU dependencies; CUDA additionally requires the existing CuPy/device environment. All commands run at the repository root. Every output directory must be new.

```sh
python tools/research/run_grid_parameter_study.py --stage freeze-history --output results/mc-grid-opt-01/history
python tools/research/run_grid_parameter_study.py --stage A --backend cpu --output results/mc-grid-opt-01/A
python tools/research/run_grid_parameter_study.py --stage B --backend cpu --previous results/mc-grid-opt-01/A --historical-manifest results/mc-grid-opt-01/history/historical-manifest.json --output results/mc-grid-opt-01/B
python tools/research/run_grid_parameter_study.py --stage C --backend cuda --previous results/mc-grid-opt-01/B --historical-manifest results/mc-grid-opt-01/history/historical-manifest.json --output results/mc-grid-opt-01/C
python tools/research/run_grid_parameter_study.py --stage D --backend cuda --previous results/mc-grid-opt-01/C --historical-manifest results/mc-grid-opt-01/history/historical-manifest.json --output results/mc-grid-opt-01/D
```

Set the certified snapshot location in `configs/research/mc_grid_opt_01.json` to its local location. The loader verifies its frozen identity. `--stage pilot --pilot-paths 16` exercises all models without selecting parameters. Pilot output is not a formal stage predecessor.

Formal model fitting uses observations completed through July 15, 2026. July 15–August 12 yields four heldout weekly windows from the same existing audited snapshot. These windows are not historically untouched: the original baseline and earlier studies used that snapshot. Frozen relative replay preserves returns while anchoring the first quote to the configuration's initial price. The protocol records open/close endpoint separation and prior use.

## Economic interpretation

Capital participation changes the committed-capital ceiling, not the wallet or a dynamic budget order. Initial inventory is endowed under USDM accounting. Legacy inventory validation additionally constrains which endowed quantities can be represented; rejected requests are retained. Fixed holdings use a t=0 limit rounded down to tick and can fill only at later closes, subject to capacity and quantity rules. Their target is one fixed order, not retrospectively matched average exposure. The endowed comparator matches initial units exactly. Neither holdings comparator has strategy risk exits.

Net return includes paid fees and execution slippage exactly once. Terminal inventory is marked, not liquidated. Funding is not simulated. A synthetic close and fixed capacity do not establish observed order-book execution quality. Peak commitment includes pending buy reservations; notional exposure and commitment are not interchangeable. Profit per mean committed capital is a finite-horizon ratio, not an annualized return or an independent source of profit.

## Selection and uncertainty

B samples 500 distinct feasible definitions. C retains 20 by Pareto layers and normalized objective coverage, evaluating four models with five independent seeds and 100,000 paths per cell. D retains the same 20 under new random domains before historical and cost stress; no post-validation retuning is permitted. Model averages are equal-weight scenario summaries, not calibrated probabilities. Averaged cell quantiles are not quantiles of a pooled predictive distribution.

The research profitability shortlist requires a positive multiplicity-corrected cash-return lower bound and activity across the three nonstress models. Jump stress remains a fully reported adverse scenario, not a calibrated probability forecast or an invented profitability veto. Seed-level Student-t intervals assume approximate normality of independent seed means; descriptive contrasts are not familywise claims. Cash-return claims use a Bonferroni family covering the 500 searched configurations and four models. The shortlist is not deployment approval; failure to enter it is not proof of universal unprofitability.

D includes 1×/2× transaction-cost and 1×/0.5× capacity historical scenarios. Independent random, equity quantile, extreme and execution-stress path audits reconcile stepwise state and fills with persisted Paper/FIFO. Audit selection is not a population-risk estimator. Full configuration definitions and per-path reductions are retained in each experiment directory.

## Completed experiment and conclusion

A evaluated 55 configurations, B evaluated 500 sampled configurations plus two baselines, and C/D each evaluated 20 locked representatives plus two baselines across four models, five seeds and 100,000 paths per cell. Formal stages used the native CPU backend; the RTX 2070 pilot and persisted Paper comparison additionally exercised CUDA.

The independent D assessment returned **zero research-profitability candidates**. No configuration met the frozen cross-model, multiplicity-corrected positive cash-return and activity rule. This is a negative result for this search and calibration, not a proof that every static grid is unprofitable. Inactivity can lie on the risk/return frontier: zero trading and zero return must not be sold as an efficient profitable strategy. No conservative/balanced/high-risk allocation is manufactured from this result.

The 20 locked configurations show strong model dependence:

| D model | Active in every seed | Positive corrected cash-return lower bound | Range of mean returns | Largest mean cell CVaR99 loss |
|---|---:|---:|---:|---:|
| GBM | 18/20 | 0/20 | −0.04732% to 0% | 2.14581% |
| Moving-block bootstrap | 18/20 | 0/20 | −0.03963% to 0% | 1.71906% |
| Regime historical | 16/20 | 18/20 | 0% to +0.04427% | 0.52787% |
| Jump-down stress | 19/20 | 0/20 | −0.80276% to 0% | 11.63969% |

Each entry averages five independent 100,000-path cells. These ranges are across configurations, not confidence intervals. The regime model's positive returns do not transfer to GBM/bootstrap; selecting on that model alone would change the frozen rule. The jump model exposes much larger loss tails and is reported without assigning it a real-world frequency.

D also completed 352 historical configuration/window/cost/capacity evaluations and 166 persisted Paper/FIFO path comparisons in 16 model/configuration groups. Historical replay is separate from fresh-seed validation and retains the previously disclosed data-use history. The frozen shortlist remains empty; historical stress is descriptive evidence, not a mechanism for post-validation promotion.

The external experiment root is `T:/mc-grid-opt-01`; the [complete report](../validation/mc-grid-opt-01/results/report.md) contains full definitions, cell distributions, paired contrasts, model-separated sensitivity/Pareto figures and five-seed uncertainty. Seed intervals for return and risk metrics are retained in the D assessment; cash-return selection uses the separate 2,000-comparison correction. The four research test modules passed together: 31 tests.

See the [research protocol](../validation/mc-grid-opt-01/PROTOCOL.md) for the frozen design.
