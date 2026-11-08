"""Synthetic software-only optimizer/execution example; no return forecasts."""
import json

import numpy as np
import pandas as pd

from factorlab.backtest import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.portfolio import OptimizationInput, OptimizerConfig, PortfolioOptimizer


def run_demo():
    dates = pd.bdate_range('2024-01-02', periods=5)
    symbols = ('000001.SZ', '600000.SH')
    optimizer = PortfolioOptimizer(OptimizerConfig(investment=.8))
    optimized = optimizer.optimize(OptimizationInput(
        symbols=symbols, decision_time=dates[0],
        covariance=pd.DataFrame(np.diag([1., 4.]), index=symbols, columns=symbols),
        covariance_as_of=dates[0] - pd.Timedelta(days=1),
        previous_weights=pd.Series(0., index=symbols)))
    targets = optimized.target_weights.rename_axis('symbol').rename('target_weight').reset_index()
    targets['date'] = dates[0]
    targets['optimizer_turnover'] = optimized.turnover
    bars = pd.DataFrame([dict(date=day, symbol=symbol, raw_open=10., raw_close=10.,
                            volume=100000, trade_status=1, is_st=False)
                         for day in dates for symbol in symbols])
    benchmark = pd.DataFrame(dict(date=dates, adj_open=100., adj_close=100.))
    result = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=10000)).run_target_weights(
        targets, bars, benchmark)
    summary = dict(synthetic=True, boundary='NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED',
                   target_weights=optimized.target_weights.to_dict(), optimizer_gross_security_turnover=optimized.turnover,
                   input_identity=optimized.input_identity,
                   actual_shares=result.positions[result.positions.date == dates[1]].set_index('symbol').shares.to_dict(),
                   final_cash=float(result.nav.iloc[-1].cash), final_nav=float(result.nav.iloc[-1].strategy_nav),
                   buy_notional=float(result.nav.buy_notional.sum()), sell_notional=float(result.nav.sell_notional.sum()),
                   first_turnover=float(result.nav.iloc[0].turnover),
                   turnover_denominator=float(result.nav.iloc[0].turnover_denominator))
    return result, summary


if __name__ == '__main__':
    result, summary = run_demo()
    print(json.dumps(summary, indent=2, sort_keys=True))
    for name in ('trades', 'positions', 'nav', 'target_comparison'):
        print(f'\n{name}\n{getattr(result, name).to_string(index=False)}')
