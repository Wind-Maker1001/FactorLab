"""Hand-specified signal interventions; synthetic expected returns are assumptions."""
import numpy as np
import pandas as pd

from factorlab.application.signal_propagation import prediction_targets
from factorlab.backtest import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.evaluation.return_attribution import attribute_execution
from factorlab.portfolio import OptimizationInput, OptimizerConfig, PortfolioOptimizer


def run_cases():
    dates=pd.bdate_range('2024-01-02',periods=8)
    symbols=('000001.SZ','000002.SZ')
    bars=pd.MultiIndex.from_product([dates,symbols],names=['date','symbol']).to_frame(index=False)
    bars=bars.assign(raw_open=10.,raw_close=10.,volume=100000.,trade_status=1,is_st=False)
    bars.loc[(bars.date>=dates[5]) & bars.symbol.eq(symbols[0]),'raw_close']=12.
    benchmark=pd.DataFrame(dict(date=dates,adj_open=100.,adj_close=100.))
    risk=pd.DataFrame({symbols[0]:[.01,-.02,.03],symbols[1]:[-.02,.01,.04]},index=dates[:3])
    receipts=[]
    for objective in ('equal','minimum_variance','score','mean_variance'):
        weights=[]
        for swapped in (False,True):
            scores=[2.,1.] if swapped else [1.,2.]
            if objective=='mean_variance':
                mu=pd.Series(np.array(scores)*.001,index=symbols)
                optimized=PortfolioOptimizer(OptimizerConfig(objective=objective,investment=.8,max_weight=1.)).optimize(
                    OptimizationInput(symbols=symbols,decision_time=dates[3],returns=risk,
                        expected_returns=mu,expected_return_definition='ASSUMED synthetic daily arithmetic returns, not model calibration'))
                target=optimized.target_weights.rename_axis('symbol').rename('target_weight').reset_index().assign(date=dates[3])
            else:
                predictions=pd.DataFrame(dict(date=dates[3],symbol=symbols,fold=0,score=scores))
                target=prediction_targets(predictions,risk,objective=objective,max_weight=1.)[0]
            weights.append(target.target_weight.to_numpy())
            for cash in (100000,100):
                result=AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=cash)).run_target_weights(target,bars,benchmark)
                attr=attribute_execution(result,initial_cash=cash,calendar=dates[4:])
                receipts.append(dict(objective=objective,swapped=swapped,initial_cash=cash,
                    target_weights=target.target_weight.tolist(),filled_shares=result.trades[['symbol','executed_shares']].to_dict('records'),
                    final_shares=result.positions.loc[result.positions.date.eq(dates[-1]),'shares'].tolist(),
                    final_nav=float(result.nav.iloc[-1].strategy_nav),**attr.summary))
        if objective in ('equal','minimum_variance'):
            np.testing.assert_allclose(weights[0],weights[1],rtol=0,atol=1e-10)
        else:
            assert not np.allclose(weights[0],weights[1])
    return receipts
