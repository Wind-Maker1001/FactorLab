"""Export non-price research tables and portable identities from a private run."""
import argparse
import json
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))
from factorlab.dataset.identity import file_hash
import importlib.util

_spec = importlib.util.spec_from_file_location('risk_comparison_plot', Path(__file__).with_name('plot_risk_comparison.py'))
_plot_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_plot_module)
plot = _plot_module.plot


def publish(source, output):
    source, output = Path(source), Path(output)
    output.mkdir(parents=True, exist_ok=False)
    frame = pd.read_csv(source / 'account-metrics.csv')
    repo = Path(__file__).resolve().parents[2]
    historical = pd.read_csv(repo / 'docs/validation/p6-r01/account-metrics.csv')
    original = frame.loc[frame.scenario.eq('original')]
    keys = ['symbol', 'year', 'strategy']
    compared = original.merge(historical, on=keys, suffixes=('_new', '_old'), validate='one_to_one')
    columns = ['cumulative_return', 'annualized_volatility', 'sharpe_ratio', 'maximum_drawdown',
               'calmar_ratio', 'average_capital_exposure', 'number_of_trades',
               'portfolio_turnover', 'total_transaction_cost']
    differences = {}
    for column in columns:
        new, old = compared[column + '_new'], compared[column + '_old']
        np.testing.assert_allclose(new, old, rtol=1e-8, atol=1e-9, equal_nan=True)
        differences[column] = float((new - old).abs().max())
    for path in source.glob('*.csv'):
        shutil.copyfile(path, output / path.name)
    for scenario in frame.scenario.unique():
        (output / scenario).mkdir()
        for path in (source / scenario).glob('*.csv'):
            shutil.copyfile(path, output / scenario / path.name)
    shutil.copyfile(source / 'config.json', output / 'preregistration.json')
    pairs = pd.read_csv(source / 'paired_accounts.csv')
    same_fill_ranking = pairs.groupby(['scenario', 'strategy']).agg(
        mean_net_return=('cumulative_return', 'mean'),
        mean_same_fill_gross=('same_fill_gross_contribution', 'mean'),
        absolute_cost_sign_reversals=('cost_reverses_absolute_sign', 'sum'),
        paired_cost_sign_reversals=('cost_reverses_pair_sign', 'sum')).reset_index()
    same_fill_ranking['net_rank'] = same_fill_ranking.groupby('scenario').mean_net_return.rank(ascending=False)
    same_fill_ranking['same_fill_gross_rank'] = same_fill_ranking.groupby('scenario').mean_same_fill_gross.rank(ascending=False)
    same_fill_ranking.to_csv(output / 'cost-rankings.csv', index=False)
    baseline_columns = ['cumulative_return', 'maximum_drawdown', 'annualized_volatility',
                        'average_capital_exposure', 'cost_to_initial_capital']
    effects = frame.loc[frame.scenario.ne('original')].merge(
        original[keys + baseline_columns], on=keys, suffixes=('', '_original'), validate='many_to_one')
    for column in baseline_columns:
        effects['change_' + column] = effects[column] - effects[column + '_original']
    effects['drawdown_improved'] = effects.maximum_drawdown.gt(effects.maximum_drawdown_original)
    effects['return_improved'] = effects.cumulative_return.gt(effects.cumulative_return_original)
    effects.to_csv(output / 'budget-effects.csv', index=False)
    effects.groupby(['scenario', 'strategy', 'year']).agg(
        mean_return=('cumulative_return', 'mean'),
        mean_return_change=('change_cumulative_return', 'mean'),
        mean_drawdown_change=('change_maximum_drawdown', 'mean'),
        drawdown_improved_fraction=('drawdown_improved', 'mean'),
        return_improved_fraction=('return_improved', 'mean')).reset_index().to_csv(
            output / 'budget-effects-annual.csv', index=False)
    plot(source / 'account-metrics.csv', output)
    receipt = json.loads((source / 'summary.json').read_text(encoding='utf-8'))
    receipt['command'] = ['python', '-I', '-B', '-X', 'utf8', 'tools/research/run_risk_matched_comparison.py',
        '--config', 'configs/research/p7-02-risk-matched-comparison.json',
        '--research-root', '<P6_D01_RELEASE>', '--source-root', '<P5_R03_SNAPSHOT>',
        '--baseline-root', '<P6_R01_PRIVATE_RUN>', '--output', '<NEW_PRIVATE_OUTPUT>']
    receipt['public_baseline_max_abs_difference'] = differences
    receipt['private_summary_sha256'] = file_hash(source / 'summary.json')
    receipt['private_artifacts_sha256'] = {
        p.relative_to(source).as_posix(): file_hash(p)
        for p in sorted(source.rglob('*.csv'))}
    receipt['publication_sources_sha256'] = {
        p.relative_to(repo).as_posix(): file_hash(p) for p in
        [Path(__file__), Path(__file__).with_name('plot_risk_comparison.py')]}
    receipt['published_artifacts_sha256'] = {p.relative_to(output).as_posix(): file_hash(p)
        for p in sorted(output.rglob('*')) if p.is_file()}
    (output / 'identity.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    print('Published', len(frame), 'rows; historical baseline matches', len(compared), 'rows')
    print(same_fill_ranking.to_string(index=False))
    print(pairs.groupby(['scenario', 'strategy']).agg(
        exposure=('average_capital_exposure', 'mean'), drawdown=('maximum_drawdown', 'mean'),
        outperform=('paired_cumulative_return', lambda x: x.gt(0).mean()),
        profitable=('cumulative_return', lambda x: x.gt(0).mean()), no_fill=('no_fill', 'sum')).to_string())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    publish(args.source, args.output)
