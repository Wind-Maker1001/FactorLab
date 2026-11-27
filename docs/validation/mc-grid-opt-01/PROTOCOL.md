# MC-GRID-OPT-01 — frozen research protocol

Status: A–D completed. A: 55 configurations and 10,000 paths/model; B: 500 sampled configurations plus two baselines and 10,000 paths/model. C and D each used 20 locked representatives plus baselines, four models, five independent seeds and 100,000 paths per cell on native CPU. D completed 352 historical stress evaluations and 166 persisted Paper/FIFO path comparisons across 16 groups. The frozen profitability shortlist is empty. The four research test modules passed together (31 tests). Independent final source/financial review found no material defects and confirmed the empty shortlist, historical evaluation count and matched audit count. The [published result bundle](results/report.md) contains definitions, distributions, paired contrasts and uncertainty figures; large retained data are external under `T:/mc-grid-opt-01`. [PR #276](https://github.com/Wind-Maker1001/FactorLab/pull/276) remains open; trusted CI evidence is pending.

Integration replay against main `a48fea581df296474845bc825f35cec861c80c3c`: the first and last persisted audit paths in each of the 16 model/configuration groups (32 frozen paths) matched the original research's 16 account columns, every fill and reduced study metrics on native CPU, RTX 2070 CUDA and static Paper at the original audit tolerances; fill/state/reason fields matched exactly. Disabling fill collection preserved traces and existing order events. All 166 saved audit records remain matched, the 500-search definition is retained and the shortlist remains empty. [Replay evidence](main-integration.json). Integration regressions passed: 108 static/dynamic/funding/input/parameter-study tests on Python 3.13, and 58 real-device CUDA parity tests, including default/explicit/disabled fill collection across static, rebuild, ATR, B and C profiles.

Storage retention: 27,929,463,296 bytes of rebuildable A/B/C and D passive-control arrays were released after reporting; final D strategy arrays, all stage definitions/seeds/source archives, complete cell summaries, historical results and persisted Paper/FIFO artifacts remain. Partial D and superseded pilot/preview outputs additionally released 737,635,021 bytes. Large experiment storage was moved off D: before final validation.

Formal stochastic calibration uses completed return endpoints through 2026-07-15T00:00:00Z from the certified MC-05 snapshot. Four nonoverlapping 168-hour historical windows cover July15–August12 and are excluded from this task's calibration/selection. Earlier studies and the old baseline used this snapshot, so these are not historically untouched samples. The early full-calibration pilot measured runtime only. Legacy GridConfig inventory validation constrains endowed-inventory coverage; invalid larger requested inventories remain recorded.

The account model is the existing long-only static USDM research simulator. Capital is 1000 USDT; initial inventory is endowed, not a paid purchase. Funding is not simulated. Terminal positions are marked, not liquidated with invented costs. Synthetic close-only fills and fixed capacity are not an order-book model.

## Design

A changes one parameter at a time around `configs/mc05_grid_tail.json`. B samples 500 distinct feasible configurations after A; each screening model uses at least 10,000 common paths. C retains 20 representatives and uses four models, five independent seeds and at least 100,000 paths per model/seed/configuration. D freezes candidates before independent seeds, historical windows and cost stress. Separate stage seed namespaces prohibit path reuse across stages. Within a stage, candidates share frozen quotes; batch size is part of generation provenance.

Parameters: arithmetic/geometric; 4/8/16/32 intervals; 5/10/20/30/40 percent lower and upper distances; 10/25/50/75/100 percent committed-capital ceilings; initial inventory and fixed order quantities in legacy units; inventory, turnover and drawdown limits; TTL 24/48/100/168 hours; pause/exit breakouts; fee, adverse slippage, capacity and venue precision/minimum rules. No budget sizing, reseeding or dynamic strategy extension.

Capital participation means `max_cash_committed / BatchGridConfig.capital`, not wallet rescaling or dynamic budget orders. Fixed order sizes are independent of this ceiling. Orders and inventory are multiplied by `quantity_unit`. Fee/slippage inputs and strategy admission estimates change together. Record inactive constraints and no-fill populations rather than condition them away.

## Stage A observations

Each model used 10,000 common paths. For the unchanged baseline, mean net return / CVaR99 loss / probability of any fill were: GBM −0.00934% / 0.47047% / 64.32%; bootstrap −0.01127% / 0.40638% / 59.05%; regime historical +0.01809% / 0.15443% / 39.56%; jump stress −0.37202% / 2.54346% / 79.79%. These are one-seed screening observations, not final confidence claims.

With GBM, narrowing the symmetric band from ±20% to ±5% increased fill probability from 64.32% to 92.52% and CVaR99 loss from 0.47047% to 1.26323%, while mean net return fell from −0.00934% to −0.03799%. Widening to ±40% lowered fills to 5.57%; its small loss is largely inactivity, not demonstrated trading efficiency. Under jump stress the ±30% and ±40% bands instead had CVaR99 losses of 4.33220% and 3.78600%, above the ±20% baseline. Thus widening is not a universally monotonic tail-risk improvement.

The coarse search retains broad parameter strata to test interactions rather than discarding levels solely on one-factor mean return. Per-model sensitivity CSV/SVG artifacts are generated by `tools/research/report_grid_parameter_study.py`; no model mixture is presented as a calibrated forecast.

## Selection and validation

Selection uses non-dominated risk/return/resource vectors and representative parameter/market-state coverage, not a unique scalar optimum. Record every tested configuration, invalid configuration and failed experiment. Never drop unsupported paths from a population. Report unconditional outcomes and separately labelled activity-conditioned diagnostics.

Primary outcomes: mean net return; loss CVaR95/CVaR99 using the existing fractional-tail convention; maximum drawdown distribution; loss and fill probabilities; maximum/mean committed capital; inventory and notional exposure; turnover and fees/slippage. Trace reductions include the initial observation. Efficiency denominators must be explicit; zero deployment is not infinite efficiency.

Comparators include unchanged MC-05 geometric, arithmetic with otherwise unchanged settings, cash, and fixed holdings with explicitly matched initial capital/inventory and separately declared additional exposure anchors. No hindsight exposure matching. Pair candidate and comparator by model/seed/path. Report seed-level uncertainty and simultaneous/multiple-search adjustments for inferential claims. Five seeds are independent replications, not five batches of a single experiment.

Existing audited historical data take precedence over new acquisition. Calibration/selection and validation timestamps and data-use history are frozen before B. Previously studied R07 windows may be held out from this task but are not historically untouched OOS. Conditional execution-case windows cannot estimate unconditional performance. Do not retune after inspecting D and reuse D as a fresh validation sample.

Paper audits sample independent random IDs, equity quantiles, extremes and execution boundary cases. Compare stepwise state and every fill; persist full Paper/FIFO evidence for final candidates. Audit samples are not tail probability estimates.

At most ten final configurations, possibly zero, with full account/grid/rule definitions, market applicability, failure conditions, cost stress and validation evidence. No live deployment or automatic merge.
