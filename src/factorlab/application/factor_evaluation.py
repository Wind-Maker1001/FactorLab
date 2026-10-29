"""Factor evaluation/report orchestration for inputs verified by the caller.

This service grants no data admission. CLI validation and run finalization stay
with the caller; calculation, reporting, target selection and logging are explicit
dependencies so the service can be used without importing the CLI.
"""

from factorlab.errors import FactorLabError


def evaluate_admitted_dataset(
    config, run, dataset, *, research_calendar, dataset_sha256, universe_version,
    evaluate, write_report, target_for_config, logger,
) -> None:
    """Evaluate a pinned dataset, retaining report/receipt/failure ordering."""
    run.payload["dataset_sha256"] = dataset_sha256
    run.payload["universe_version"] = universe_version
    target = target_for_config(config)
    evaluation = evaluate(
        dataset,
        config.factors,
        target=target,
        quantiles=config.evaluation.quantiles,
        minimum_cross_section=config.evaluation.min_cross_section,
        group_column=("MARKET_REGIME" if config.market_regime.enabled else None),
        calendar=research_calendar,
    )
    summary = write_report(evaluation, config.paths.factor_results_dir)
    run.add_output("factor_report", summary)
    valid_rank_ic = int(evaluation.daily_ic["RankIC"].notna().sum())
    run.payload["valid_rank_ic_observations"] = valid_rank_ic
    if valid_rank_ic == 0:
        raise FactorLabError("Factor evaluation produced no valid RankIC observations")
    logger.info("Factor report written with %d valid daily RankIC values", valid_rank_ic)
