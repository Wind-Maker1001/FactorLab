"""Public derivative CSV columns: no stock-level identifiers, prices or labels."""

CSV_COLUMNS = {
    "daily_input_audit.csv": "date factor market_regime RankIC IC factor_available observable_labels pair_count valid_rank_ic missing_reason".split(),
    "quintile_daily.csv": "date factor quantile eligible_factor_count observable_factor_count label_missing_rate A_formed A_members A_observable_members A_mean A_bucket_missing_rate B_formed B_members B_observable_members B_mean B_bucket_missing_rate observable_members_changed all_members_changed observable_member_overlap B_minus_A_mean".split(),
    "quintile_monthly.csv": "factor month quantile paired_dates mean_B_minus_A max_abs_difference mean_label_missing_rate changed_member_dates".split(),
    "quintile_spread.csv": "date factor label_missing_rate eligible_factor_count observable_factor_count A_Q5_minus_Q1 B_Q5_minus_Q1 B_minus_A_spread".split(),
    "rankic_descriptive.csv": "factor mean std valid_dates positive_ratio calendar_dates valid_runs coverage iid_se_descriptive_only".split(),
    "rankic_inference.csv": "factor method valid_dates sample_insufficient estimate se ci_low ci_high p_two_sided numerical_status valid_repeats bootstrap_bias p_holm conditional_support".split(),
    "regime_comparisons.csv": "factor comparison left_valid_dates right_valid_dates sample_insufficient method estimate se ci_low ci_high p_two_sided numerical_status valid_repeats bootstrap_bias p_holm conditional_support".split(),
    "regime_intervals.csv": "interval_id state start end signal_dates factor valid_dates coverage".split(),
    "regime_means.csv": "factor state mean signal_dates valid_dates signal_runs valid_runs coverage sample_insufficient method se ci_low ci_high p_two_sided numerical_status valid_repeats bootstrap_bias".split(),
    "rankic_method_sensitivity.csv": ["factor"]
    + [
        method + "_" + metric
        for metric in ("se", "p_two_sided", "ci_low", "ci_high")
        for method in ("HAC20", "HAC5", "MBB20")
    ]
    + ["HAC20_over_HAC5_se", "MBB20_over_HAC20_se"],
}
