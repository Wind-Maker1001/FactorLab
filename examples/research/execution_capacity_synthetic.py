"""Small reproducible capacity sensitivity experiment; no model fitting."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
import time

import numpy as np
import pandas as pd

from factorlab.application.research_pipeline import feedback_execution, BOUNDARY
from factorlab.backtest.capacity import LiquidityObservation, ResearchExecutionScenario
from factorlab.backtest.execution import AShareExecutionBacktester, ExecutionBacktestConfig
from factorlab.dataset.identity import file_hash, frame_hash
from factorlab.evaluation.execution_risk import execution_diagnostics, order_outcomes, paired_constraint_difference
from factorlab.evaluation.research import EvaluationContext, ResearchEvaluation, reprice_costs
from factorlab.evaluation import return_attribution
from factorlab.backtest.fees import FeeSchedule
from factorlab.metrics.exposures import diagnose_exposures
from factorlab.portfolio import PortfolioOptimizer, OptimizerConfig


def fixture():
    days = pd.bdate_range('2024-01-02', periods=32)
    symbols = ['000001.SZ', '000002.SZ', '600000.SH']
    bars, predictions, evidence, liquidity = [], [], [], []
    for i, day in enumerate(days):
        for j, symbol in enumerate(symbols):
            close = (10 + j) * (1 + .001 * i + .005 * np.sin(i * .7 + j))
            opening = (10 + j) * (1 + .001 * i + .005 * np.sin((i - .3) * .7 + j))
            bars.append(dict(date=day, symbol=symbol, raw_open=opening, raw_close=close,
                             volume=10000 + 500 * j, trade_status=1, is_st=False))
            if 6 <= i < len(days) - 1:
                predictions.append(dict(date=day, symbol=symbol, score=float(np.sin(i / 3 + j * 2)), fold=0))
            if i >= 5:
                # Historical averages exclude the execution day and are known at this close.
                prior = [r for r in bars if r['symbol'] == symbol and r['date'] <= day][-5:]
                liquidity.append(LiquidityObservation(symbol, str(day.date()),
                    str(day + pd.Timedelta(hours=15)), float(np.mean([r['volume'] for r in prior])),
                    'shares', 'synthetic trailing five-session ADV', 'SYNTHETIC'))
                for kind, value, unit in [('industry', ['Technology', 'Finance', 'Finance'][j], 'category'),
                    ('market_cap', (j + 1) * 1e9, 'CNY'),
                    ('momentum', float(close / ((10 + j) * (1 + .001 * (i-5) + .005 * np.sin((i-5) * .7+j))) - 1), 'return'),
                    ('volatility', .01 + j * .002, 'daily_return_std')]:
                    evidence.append(dict(symbol=symbol, effective_date=day, known_at=day + pd.Timedelta(hours=15),
                        source='synthetic disclosed style assumptions', source_kind='SYNTHETIC', kind=kind, value=value, unit=unit))
    bars = pd.DataFrame(bars)
    returns = bars.pivot(index='date', columns='symbol', values='raw_close').pct_change().dropna()
    benchmark = pd.DataFrame(dict(date=days, adj_open=100 + np.arange(len(days)) * .1,
                                  adj_close=100.05 + np.arange(len(days)) * .1))
    return bars, benchmark, pd.DataFrame(predictions), returns, tuple(liquidity), pd.DataFrame(evidence)


def experiment(output, capitals=(10000, 100000, 1000000), participation=(.02, .10)):
    started = time.perf_counter()
    output.mkdir(parents=True, exist_ok=False)
    bars, benchmark, predictions, returns, liquidity, evidence = fixture()
    first = predictions.date.min()
    execution_bars, execution_benchmark = bars[bars.date >= first], benchmark[benchmark.date >= first]
    calendar = pd.DatetimeIndex(execution_benchmark.date)[1:]
    summaries, differences, exposures = [], [], []
    evaluation = ResearchEvaluation(EvaluationContext(calendar=tuple(calendar.strftime('%Y-%m-%d')),
        dataset_id='synthetic-capacity-v1', universe_id='three-synthetic-securities', label='not_used',
        label_id='not_used', benchmark_id=frame_hash(benchmark), holding_days=1, source_kind='SYNTHETIC'))
    for capital in capitals:
        reference = None
        scenarios = [('base', None), ('zero', ResearchExecutionScenario())] + [
            (f'participation-{limit:g}', ResearchExecutionScenario(fixed_slippage_bps=2,
                participation_limit=limit, impact_coefficient_bps=20, liquidity=liquidity)) for limit in participation]
        for name, scenario in scenarios:
            targets, decisions, result = feedback_execution(predictions, returns, model_name='fixed_synthetic_scores',
                optimizer=PortfolioOptimizer(OptimizerConfig(objective='score', investment=.8, max_weight=.5)),
                lookback=5, every=3,
                engine=AShareExecutionBacktester(ExecutionBacktestConfig(initial_cash=capital), research_scenario=scenario),
                bars=execution_bars, benchmark=execution_benchmark)
            turnover = decisions[['date', 'estimated_gross_security_turnover']].rename(
                columns={'date': 'signal_date', 'estimated_gross_security_turnover': 'target_turnover'})
            attributed = execution_diagnostics(result, initial_cash=capital, calendar=calendar,
                predictions=predictions, decisions=turnover)
            evaluation.portfolio(name, result.nav, trades=result.trades, positions=result.positions)
            risk = diagnose_exposures(result.positions, evidence, calendar=calendar)
            risk['capital'], risk['scenario'] = capital, name
            exposures.append(risk)
            # The executable chain retains all layers, rather than only aggregate NAV.
            directory = output / f'{capital}-{name}'
            directory.mkdir()
            for key, frame in dict(predictions=predictions, targets=targets, decisions=decisions,
                trades=result.trades, rejections=result.rejections, positions=result.positions,
                target_comparison=result.target_comparison, attribution=attributed.daily,
                execution=attributed.execution, orders=order_outcomes(result)).items():
                frame.to_csv(directory / (key + '.csv'), index=False)
            if name == 'base':
                reference = attributed
                base_result = result
                fixed = reprice_costs(result.trades, {'base': FeeSchedule.a_share_default(),
                    'high_slippage': FeeSchedule.a_share_default(slippage_bps=25)})
                fixed.to_csv(directory / 'fixed-fills-costs.csv', index=False)
                assert (fixed.loc[fixed.scenario == 'high_slippage', 'cost_delta'] >= 0).all()
            elif name == 'zero':
                for key in ('nav', 'trades', 'positions', 'rejections', 'target_comparison'):
                    pd.testing.assert_frame_equal(getattr(base_result, key), getattr(result, key))
            else:
                contrast = paired_constraint_difference(attributed, reference)
                contrast['capital'], contrast['scenario'] = capital, name
                differences.append(contrast)
            summaries.append(dict(capital=capital, scenario=name, source_kind='SYNTHETIC',
                qualification='SOFTWARE_ONLY_NOT_REAL_STRATEGY_CAPACITY', **attributed.summary))
    pd.concat(exposures).to_csv(output / 'exposures.csv', index=False)
    pd.concat(differences).to_csv(output / 'counterfactual-differences.csv', index=False)
    predictions.to_csv(output / 'predictions.csv', index=False)
    evidence.to_csv(output / 'style-evidence.csv', index=False)
    protocol = dict(boundary=BOUNDARY, source_kind='SYNTHETIC', capitals=list(capitals),
        participation=list(participation), liquidity=[asdict(x) for x in liquidity],
        predictions_identity=frame_hash(predictions), bars_identity=frame_hash(bars),
        attribution_source_sha256=file_hash(Path(return_attribution.__file__)),
        attribution_api='factorlab.evaluation.return_attribution.attribute_execution',
        elapsed_seconds=time.perf_counter() - started, python=sys.version,
        accounting='R14 verifies every fill cash flow, daily costs/cash/equity/NAV and cumulative security shares',
        results=summaries)
    (output / 'results.json').write_text(json.dumps(protocol, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    pd.DataFrame(summaries).to_csv(output / 'summary.csv', index=False)
    print(json.dumps(dict(output=str(output), scenarios=len(summaries), elapsed_seconds=protocol['elapsed_seconds']), indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--capitals', type=float, nargs='+', default=[10000, 100000, 1000000])
    parser.add_argument('--participation', type=float, nargs='+', default=[.02, .10])
    args = parser.parse_args()
    experiment(args.output, tuple(args.capitals), tuple(args.participation))
