# R06 收益与风险表

NO LIVE MONEY · EXPLORATORY_RESEARCH

金额 USDT；收益率/回撤/持仓比例为小数；全部 100 个固定目标、所有动态对照及风险统计见 metrics.csv。

| scenario | name | net_pnl | net_return | max_drawdown | mean_notional | max_notional | max_committed | holding_fraction | fills | turnover_usdt | total_cost | completed_cycle_net | residual_net |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base | fixed_012 | -1.221399 | -0.001221 | 0.005981 | 25.055408 | 28.718400 | 27.508999 | 0.999537 | 2.000000 | 27.408835 | 0.027399 | — | -1.221399 |
| base | fixed_013 | -1.323182 | -0.001323 | 0.006479 | 27.143358 | 31.111600 | 29.801416 | 0.999537 | 2.000000 | 29.692905 | 0.029682 | — | -1.323182 |
| base | cash_arithmetic | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 |
| base | fixed_budget_buy_hold_arithmetic | -10.178325 | -0.010178 | 0.049376 | 208.795065 | 239.320000 | 229.241660 | 0.999537 | 10.000000 | 228.406962 | 0.228325 | 0.000000 | -10.178325 |
| base | B_arithmetic | 8.911795 | 0.008912 | 0.011928 | 67.990911 | 121.578000 | 215.505019 | 0.929167 | 20.000000 | 416.232800 | 0.416205 | 0.940091 | 7.971704 |
| base | B_geometric | 4.949997 | 0.004950 | 0.004335 | 25.778522 | 63.144000 | 173.760763 | 0.785185 | 17.000000 | 355.008916 | 0.355003 | 5.115978 | -0.165981 |
| base | lag_B_geometric | 5.386149 | 0.005386 | 0.004189 | 25.797265 | 63.285000 | 62.646564 | 0.785185 | 17.000000 | 354.856655 | 0.354851 | — | 0.025210 |
| base | B_atr | 1.550942 | 0.001551 | 0.020948 | 92.292275 | 115.825000 | 132.011606 | 0.940741 | 23.000000 | 505.096262 | 0.505058 | 1.169638 | 0.381304 |
| base | D_arithmetic | 8.911795 | 0.008912 | 0.011928 | 67.990911 | 121.578000 | 215.505019 | 0.929167 | 20.000000 | 416.232800 | 0.416205 | 0.940091 | 7.971704 |
| base | D_geometric | 4.949997 | 0.004950 | 0.004335 | 25.778522 | 63.144000 | 173.760763 | 0.785185 | 17.000000 | 355.008916 | 0.355003 | 5.115978 | -0.165981 |
| base | lag_D_geometric | 5.386149 | 0.005386 | 0.004189 | 25.797265 | 63.285000 | 62.646564 | 0.785185 | 17.000000 | 354.856655 | 0.354851 | — | 0.025210 |
| base | D_atr | 0.965285 | 0.000965 | 0.001833 | 2.857143 | 108.750000 | 132.011606 | 0.076389 | 28.000000 | 613.714053 | 0.613715 | 1.169638 | 0.000000 |
| stress | fixed_012 | -1.248811 | -0.001249 | 0.005982 | 25.055408 | 28.718400 | 27.536812 | 0.999537 | 2.000000 | 27.425271 | 0.054811 | — | -1.248811 |
| stress | fixed_013 | -1.352879 | -0.001353 | 0.006479 | 27.143358 | 31.111600 | 29.831546 | 0.999537 | 2.000000 | 29.710710 | 0.059379 | — | -1.352879 |
| stress | cash_arithmetic | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 | 0.000000 |
| stress | fixed_budget_buy_hold_arithmetic | -10.406759 | -0.010407 | 0.049387 | 208.795065 | 239.320000 | 229.473432 | 0.999537 | 10.000000 | 228.543924 | 0.456759 | 0.000000 | -10.406759 |
| stress | B_arithmetic | 7.607336 | 0.007607 | 0.012002 | 68.653492 | 121.578000 | 215.607281 | 0.958333 | 18.000000 | 372.388771 | 0.744664 | -0.285034 | 7.892370 |
| stress | B_geometric | 4.630056 | 0.004630 | 0.004342 | 25.769260 | 63.144000 | 173.834527 | 0.785185 | 17.000000 | 354.983790 | 0.709944 | 4.818029 | -0.187973 |
| stress | lag_B_geometric | 5.065359 | 0.005065 | 0.004196 | 25.788019 | 63.285000 | 62.656109 | 0.785185 | 17.000000 | 354.832268 | 0.709641 | — | 0.003409 |
| stress | B_atr | 0.846686 | 0.000847 | 0.008418 | 38.389758 | 86.960000 | 132.073212 | 0.940741 | 16.000000 | 352.187536 | 0.704314 | 0.568325 | 0.278360 |
| stress | D_arithmetic | 7.607336 | 0.007607 | 0.012002 | 68.653492 | 121.578000 | 215.607281 | 0.958333 | 18.000000 | 372.388771 | 0.744664 | -0.285034 | 7.892370 |
| stress | D_geometric | 4.630056 | 0.004630 | 0.004342 | 25.769260 | 63.144000 | 173.834527 | 0.785185 | 17.000000 | 354.983790 | 0.709944 | 4.818029 | -0.187973 |
| stress | lag_D_geometric | 5.065359 | 0.005065 | 0.004196 | 25.788019 | 63.285000 | 62.656109 | 0.785185 | 17.000000 | 354.832268 | 0.709641 | — | 0.003409 |
| stress | D_atr | 0.582753 | 0.000583 | 0.001922 | 2.585511 | 86.960000 | 132.073212 | 0.075000 | 18.000000 | 395.622351 | 0.791247 | 0.568325 | 0.000000 |

## 同口径收益差

| scenario | grid | benchmark | benchmark_kind | grid_mean | benchmark_mean | grid_pnl | benchmark_pnl | advantage | bracket_status |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| base | B_arithmetic | fixed_032 | EX_ANTE_FIXED | 67.990911 | 66.814421 | 8.911795 | -3.257064 | 12.168859 | BRACKETED |
| base | B_arithmetic | fixed_033 | EX_ANTE_FIXED | 67.990911 | 68.902371 | 8.911795 | -3.358847 | 12.270642 | BRACKETED |
| base | B_arithmetic | lag_B_arithmetic | EX_ANTE_GRID_DEPENDENT_LAGGED | 67.990911 | 67.892469 | 8.911795 | 8.869705 | 0.042090 | BRACKETED |
| base | B_geometric | fixed_012 | EX_ANTE_FIXED | 25.778522 | 25.055408 | 4.949997 | -1.221399 | 6.171396 | BRACKETED |
| base | B_geometric | fixed_013 | EX_ANTE_FIXED | 25.778522 | 27.143358 | 4.949997 | -1.323182 | 6.273179 | BRACKETED |
| base | B_geometric | lag_B_geometric | EX_ANTE_GRID_DEPENDENT_LAGGED | 25.778522 | 25.797265 | 4.949997 | 5.386149 | -0.436152 | BRACKETED |
| base | B_atr | fixed_044 | EX_ANTE_FIXED | 92.292275 | 91.869829 | 1.550942 | -4.478463 | 6.029405 | BRACKETED |
| base | B_atr | fixed_045 | EX_ANTE_FIXED | 92.292275 | 93.957779 | 1.550942 | -4.580246 | 6.131188 | BRACKETED |
| base | B_atr | lag_B_atr | EX_ANTE_GRID_DEPENDENT_LAGGED | 92.292275 | 92.203435 | 1.550942 | 1.363023 | 0.187919 | BRACKETED |
| base | D_arithmetic | fixed_032 | EX_ANTE_FIXED | 67.990911 | 66.814421 | 8.911795 | -3.257064 | 12.168859 | BRACKETED |
| base | D_arithmetic | fixed_033 | EX_ANTE_FIXED | 67.990911 | 68.902371 | 8.911795 | -3.358847 | 12.270642 | BRACKETED |
| base | D_arithmetic | lag_D_arithmetic | EX_ANTE_GRID_DEPENDENT_LAGGED | 67.990911 | 67.892469 | 8.911795 | 8.869705 | 0.042090 | BRACKETED |
| base | D_geometric | fixed_012 | EX_ANTE_FIXED | 25.778522 | 25.055408 | 4.949997 | -1.221399 | 6.171396 | BRACKETED |
| base | D_geometric | fixed_013 | EX_ANTE_FIXED | 25.778522 | 27.143358 | 4.949997 | -1.323182 | 6.273179 | BRACKETED |
| base | D_geometric | lag_D_geometric | EX_ANTE_GRID_DEPENDENT_LAGGED | 25.778522 | 25.797265 | 4.949997 | 5.386149 | -0.436152 | BRACKETED |
| base | D_atr | fixed_003 | EX_ANTE_FIXED | 2.857143 | 6.263852 | 0.965285 | -0.305350 | 1.270635 | OUTSIDE_FEASIBLE_LADDER |
| base | D_atr | lag_D_atr | EX_ANTE_GRID_DEPENDENT_LAGGED | 2.857143 | 2.828679 | 0.965285 | 0.799344 | 0.165941 | OUTSIDE_FEASIBLE_LADDER |
| stress | B_arithmetic | fixed_032 | EX_ANTE_FIXED | 68.653492 | 66.814421 | 7.607336 | -3.330163 | 10.937499 | BRACKETED |
| stress | B_arithmetic | fixed_033 | EX_ANTE_FIXED | 68.653492 | 68.902371 | 7.607336 | -3.434231 | 11.041567 | BRACKETED |
| stress | B_arithmetic | lag_B_arithmetic | EX_ANTE_GRID_DEPENDENT_LAGGED | 68.653492 | 68.502826 | 7.607336 | 7.984177 | -0.376840 | BRACKETED |
| stress | B_geometric | fixed_012 | EX_ANTE_FIXED | 25.769260 | 25.055408 | 4.630056 | -1.248811 | 5.878867 | BRACKETED |
| stress | B_geometric | fixed_013 | EX_ANTE_FIXED | 25.769260 | 27.143358 | 4.630056 | -1.352879 | 5.982935 | BRACKETED |
| stress | B_geometric | lag_B_geometric | EX_ANTE_GRID_DEPENDENT_LAGGED | 25.769260 | 25.788019 | 4.630056 | 5.065359 | -0.435302 | BRACKETED |
| stress | B_atr | fixed_018 | EX_ANTE_FIXED | 38.389758 | 37.583112 | 0.846686 | -1.873217 | 2.719902 | BRACKETED |
| stress | B_atr | fixed_019 | EX_ANTE_FIXED | 38.389758 | 39.671062 | 0.846686 | -1.977284 | 2.823970 | BRACKETED |
| stress | B_atr | lag_B_atr | EX_ANTE_GRID_DEPENDENT_LAGGED | 38.389758 | 38.360535 | 0.846686 | 0.969440 | -0.122754 | BRACKETED |
| stress | D_arithmetic | fixed_032 | EX_ANTE_FIXED | 68.653492 | 66.814421 | 7.607336 | -3.330163 | 10.937499 | BRACKETED |
| stress | D_arithmetic | fixed_033 | EX_ANTE_FIXED | 68.653492 | 68.902371 | 7.607336 | -3.434231 | 11.041567 | BRACKETED |
| stress | D_arithmetic | lag_D_arithmetic | EX_ANTE_GRID_DEPENDENT_LAGGED | 68.653492 | 68.502826 | 7.607336 | 7.984177 | -0.376840 | BRACKETED |
| stress | D_geometric | fixed_012 | EX_ANTE_FIXED | 25.769260 | 25.055408 | 4.630056 | -1.248811 | 5.878867 | BRACKETED |
| stress | D_geometric | fixed_013 | EX_ANTE_FIXED | 25.769260 | 27.143358 | 4.630056 | -1.352879 | 5.982935 | BRACKETED |
| stress | D_geometric | lag_D_geometric | EX_ANTE_GRID_DEPENDENT_LAGGED | 25.769260 | 25.788019 | 4.630056 | 5.065359 | -0.435302 | BRACKETED |
| stress | D_atr | fixed_003 | EX_ANTE_FIXED | 2.585511 | 6.263852 | 0.582753 | -0.312203 | 0.894956 | OUTSIDE_FEASIBLE_LADDER |
| stress | D_atr | lag_D_atr | EX_ANTE_GRID_DEPENDENT_LAGGED | 2.585511 | 2.586480 | 0.582753 | 0.703511 | -0.120758 | OUTSIDE_FEASIBLE_LADDER |
