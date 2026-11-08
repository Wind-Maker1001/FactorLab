"""Two close decisions with exact fills, cash, fees and actual-holdings feedback."""
from datetime import date
import json

import numpy as np
import pandas as pd

from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.portfolio.feedback import ExecutionFeedback
from factorlab.portfolio.optimizer import OptimizationInput, OptimizerConfig, PortfolioOptimizer


def main():
    days = pd.bdate_range('2024-01-02', periods=3)
    symbols = ('000001.SZ', '000002.SZ')
    bars = pd.DataFrame([dict(date=day, symbol=s, raw_open=10.+i, raw_close=10.+i,
        volume=100000., trade_status=1, is_st=False) for day in days for i, s in enumerate(symbols)])
    benchmark = pd.DataFrame(dict(date=days, adj_open=100., adj_close=100.))
    engine = AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=10000),
        FeeSchedule([FeeRule(date(2000,1,1), commission_bps=0, minimum_commission_cny=5, slippage_bps=0)]))
    session = engine.start_session(bars, benchmark, signal_date=days[0])
    feedback = ExecutionFeedback(session, PortfolioOptimizer(OptimizerConfig(
        objective='score', investment=.5, turnover_limit=.5)))
    decisions = []
    for scores in [(1.,1.), (0.,1.)]:
        stamp = session.snapshot.as_of
        result = feedback.optimize(OptimizationInput(symbols, stamp,
            covariance=pd.DataFrame(np.eye(2), index=symbols, columns=symbols), covariance_as_of=stamp,
            scores=pd.Series(scores, index=symbols)))
        snap = feedback.advance()
        decisions.append(dict(decision_time=str(result.decision_time), signal_time=str(result.signal_time),
            execution_time=str(result.execution_time),
            previous_actual_weights=result.snapshot.security_weights.to_dict(),
            previous_cash_weight=result.snapshot.cash_weight,
            target_weights=result.optimization.target_weights.to_dict(),
            target_cash_weight=result.optimization.cash_weight,
            optimizer_gross_turnover=result.optimization.turnover,
            actual_shares={h.symbol:h.shares for h in snap.holdings}, cash=snap.cash, nav=snap.nav,
            actual_weights=snap.security_weights.to_dict(),
            fill_reports=[{k: None if pd.isna(v) else v for k, v in r.items()} for r in snap.last_fills],
            rejections=[{k: None if pd.isna(v) else v for k, v in r.items()} for r in snap.last_rejections]))
    assert decisions[0]['cash'] == 5790 and decisions[0]['nav'] == 9990
    assert decisions[1]['cash'] == 5580 and decisions[1]['nav'] == 9980
    assert decisions[0]['actual_shares'] == dict.fromkeys(symbols, 200)
    assert decisions[1]['actual_shares'] == {symbols[1]:400}
    assert abs(decisions[1]['optimizer_gross_turnover'] - (.5-200/9990)) < 1e-12
    print(json.dumps(dict(status='P310_SYNTHETIC_DETERMINISTIC',
        admission='NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED',
        corporate_actions_status='UNKNOWN', decisions=decisions,
        nav=session.result.nav.to_dict('records')), indent=2, default=str, allow_nan=False))


if __name__ == '__main__':
    main()
