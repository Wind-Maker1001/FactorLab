"""Publish derived account summaries and static table-backed research figures."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
import numpy as np
import pandas as pd
from factorlab.strategies.robustness_real import save
from factorlab.strategies.real_comparison import NAMES


def correlation(left, right):
    """Constant series have undefined correlation, represented as null on export."""
    return left.corr(right) if left.nunique() > 1 and right.nunique() > 1 else np.nan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    source, output = args.input, args.output
    output.mkdir(parents=True, exist_ok=True)
    summary = json.loads((source / 'summary.json').read_text(encoding='utf-8'))
    observations = pd.read_json(source / 'observations.json')
    observations.to_csv(output / 'account-metrics.csv', index=False)
    for name, rows in summary['tables'].items():
        pd.DataFrame(rows).to_csv(output / (name + '.csv'), index=False)
    explanations, episodes, environments = [], [], []
    for (symbol, year), group in observations.groupby(['symbol', 'year']):
        folder = source / f'{symbol}-{year}'
        benchmark = pd.read_csv(folder / 'buy_hold_nav.csv')
        logsteps = np.log1p(benchmark.strategy_return)
        efficiency = abs(logsteps.sum()) / abs(logsteps).sum()
        for row in group.itertuples():
            nav = pd.read_csv(folder / f'{row.strategy}_nav.csv')
            pnl = nav.portfolio_value.diff().fillna(nav.portfolio_value.iloc[0] - 100000)
            exposed = nav.shares.gt(0)
            active = exposed | exposed.shift(1, fill_value=False)
            identifiers = (exposed & ~exposed.shift(1, fill_value=False)).cumsum()
            episode_pnl = pnl.loc[active].groupby(identifiers.loc[active]).sum()
            episodes.append(dict(symbol=symbol, year=int(year), strategy=row.strategy,
                episodes=len(episode_pnl), largest_episode_pnl=float(episode_pnl.max()) if len(episode_pnl) else None,
                smallest_episode_pnl=float(episode_pnl.min()) if len(episode_pnl) else None,
                largest_absolute_episode_share=float(episode_pnl.abs().max()/episode_pnl.abs().sum()) if len(episode_pnl) and episode_pnl.abs().sum() else None,
                positive_episode_fraction=float(episode_pnl.gt(0).mean()) if len(episode_pnl) else None))
            down = benchmark.strategy_return.lt(0)
            environments.append(dict(symbol=symbol, year=int(year), strategy=row.strategy,
                benchmark_return=row.benchmark_return, path_efficiency=float(efficiency),
                cumulative_return=row.cumulative_return, exposure=row.average_capital_exposure,
                pnl_on_benchmark_down_days=float(pnl.loc[down].sum()/100000),
                pnl_on_other_days=float(pnl.loc[~down].sum()/100000)))
    environment = pd.DataFrame(environments)
    for name, group in observations.groupby('strategy'):
        env = environment.loc[environment.strategy.eq(name)]
        explanations.append(dict(strategy=name,
            correlation_return_benchmark=correlation(group.cumulative_return, group.benchmark_return),
            correlation_return_exposure=correlation(group.cumulative_return, group.average_capital_exposure),
            correlation_return_trades=correlation(group.cumulative_return, group.number_of_trades),
            correlation_return_cost=correlation(group.cumulative_return, group.cost_to_initial_capital),
            correlation_return_path_efficiency=env.cumulative_return.corr(env.path_efficiency),
            mean_down_day_pnl=env.pnl_on_benchmark_down_days.mean(),
            mean_other_day_pnl=env.pnl_on_other_days.mean()))
    pd.DataFrame(episodes).to_csv(output / 'holding-episodes.csv', index=False)
    environment.to_csv(output / 'market-environments.csv', index=False)
    for metric in ('benchmark_return', 'path_efficiency'):
        # Post-run descriptive strata never select samples or modify signals.
        environment['group'] = pd.cut(environment[metric], [-np.inf,0,np.inf], labels=['negative','nonnegative']) if metric == 'benchmark_return' else pd.cut(environment[metric],[-np.inf,.1,np.inf],labels=['low_efficiency','higher_efficiency'])
        environment.groupby(['strategy','group'], observed=True).agg(accounts=('cumulative_return','size'), mean_return=('cumulative_return','mean'), median_return=('cumulative_return','median')).reset_index().to_csv(output / f'{metric}-groups.csv', index=False)
    summary['relationships'] = json.loads(pd.DataFrame(explanations).to_json(orient='records'))
    summary['observations'] = json.loads(observations.to_json(orient='records'))
    save(output / 'real-summary.json', summary)
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.ticker import PercentFormatter
    labels = dict(dual_ma='MA5/20', bollinger='Bollinger', rsi='RSI', macd='MACD', cta='Donchian/ATR', buy_hold='Buy and hold')
    fig, axes = plt.subplots(2,3,figsize=(12,7),sharex=True,sharey=True,layout='constrained')
    fig.set_facecolor('#fcfcfb')
    for name, ax in zip(NAMES, axes.flat):
        rows = observations.loc[observations.strategy.eq(name)]
        ax.set_facecolor('#fcfcfb')
        ax.scatter(rows.average_capital_exposure, rows.cumulative_return, s=40, c='#2a78d6', edgecolors='#fcfcfb', linewidths=1.5)
        ax.axhline(0, color='#c3c2b7', linewidth=.7)
        ax.grid(axis='y',color='#e1e0d9',linewidth=.6)
        ax.spines[['top','right']].set_visible(False)
        ax.set_title(labels[name],loc='left',color='#0b0b0b')
        ax.tick_params(colors='#52514e')
        ax.xaxis.set_major_formatter(PercentFormatter(1))
        ax.yaxis.set_major_formatter(PercentFormatter(1))
    fig.supxlabel('Mean invested market value / account equity')
    fig.supylabel('Annual raw-price return net of trading costs')
    fig.suptitle('54 independent annual accounts | 22 securities | 2016–2018', color='#0b0b0b')
    fig.savefig(output/'exposure-returns.png',dpi=150)
    plt.close(fig)
    print(json.dumps(dict(status=summary['status'], relationships=summary['relationships']),ensure_ascii=False))


if __name__ == '__main__':
    main()
