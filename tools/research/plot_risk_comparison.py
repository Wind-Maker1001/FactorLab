"""Render fixed-budget exposure/return facets from saved account metrics."""
import argparse
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
import pandas as pd


def plot(source, output):
    frame = pd.read_csv(source)
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    fields = ['scenario', 'budget', 'symbol', 'year', 'industry', 'strategy',
              'average_capital_exposure', 'cumulative_return', 'maximum_drawdown', 'cost_to_initial_capital']
    frame[fields].to_csv(output / 'exposure-returns.csv', index=False)
    strategies = [('dual_ma', 'MA5/20'), ('rsi', 'RSI'), ('macd', 'MACD'),
                  ('bollinger', 'Bollinger'), ('cta', 'Donchian/ATR CTA'), ('buy_hold', 'Buy & hold')]
    for mode, surface, ink, grid, colors in [
        ('light', '#fcfcfb', '#52514e', '#e1e0d9', ['#2a78d6', '#eb6834', '#1baf7a']),
        ('dark', '#1a1a19', '#f0efec', '#494945', ['#3987e5', '#d95926', '#199e70'])]:
        fig, axes = plt.subplots(2, 3, figsize=(14, 9), sharex=True, sharey=True, layout='constrained')
        fig.set_facecolor(surface)
        for ax, (strategy, label) in zip(axes.flat, strategies):
            ax.set_facecolor(surface)
            ax.grid(color=grid, linewidth=.6, zorder=0)
            ax.axhline(0, color=ink, linewidth=.7)
            ax.spines[['top', 'right']].set_visible(False)
            ax.spines[['bottom', 'left']].set_color(grid)
            ax.tick_params(colors=ink)
            ax.set_title(label, loc='left', color=ink)
            scenarios = [('original', 'Original', '#91918b', 'x'),
                         ('budget_0.1', '10% entry budget', colors[0], 'o'),
                         ('budget_0.25', '25% entry budget', colors[1], 's'),
                         ('budget_0.5', '50% entry budget', colors[2], '^')]
            for scenario, title, color, marker in scenarios:
                part = frame.loc[frame.strategy.eq(strategy) & frame.scenario.eq(scenario)]
                ax.scatter(part.average_capital_exposure, part.cumulative_return, s=64,
                           marker=marker, c=color, alpha=.65, linewidths=.7,
                           **({} if marker == 'x' else dict(edgecolors=surface)), label=title, zorder=2)
            ax.xaxis.set_major_formatter(PercentFormatter(1))
            ax.yaxis.set_major_formatter(PercentFormatter(1))
        handles, labels = axes.flat[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc='outside lower center', ncol=4, frameon=False, labelcolor=ink)
        fig.suptitle('Frozen 54 security-year accounts | entry budgets are not equal realized risk', color=ink)
        for ax in axes[-1]:
            ax.set_xlabel('Actual mean capital exposure', color=ink)
        fig.supylabel('Raw-price return net of trading costs', color=ink)
        fig.savefig(output / f'exposure-returns-{mode}.png', dpi=140, facecolor=surface,
                    metadata={'Software': 'FactorLab'})
        plt.close(fig)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    plot(args.source, args.output)
