"""Publish bounded evidence from a completed study without repeating training/replay."""
import argparse
import json
from pathlib import Path

from .binance_model_study import PARAMS, ledger_summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    evidence = json.loads((args.study/'evidence.json').read_text(encoding='utf-8'))
    for name, result in evidence['paper'].items():
        result.update(ledger_summary(args.study/f'{name}.sqlite', result))
        result['net_return_minus_no_trade'] = result['paper_return']
        result['net_return_minus_dual_ma'] = result['paper_return']-evidence['paper']['dual_ma']['paper_return']
        result['fee_addback_return_not_cost_free_counterfactual'] = result['paper_return']+float(result['total_fees'])/1000
    evidence['conclusions'] = ['P7_R01_RESEARCH_REPRODUCIBLE','P7_R01_EVIDENCE_LIMITED','INSUFFICIENT_HISTORY']
    if all(evidence['quality'][name]['test']['mse_skill'] <= 0 and evidence['paper'][name]['paper_return'] <= 0
           for name in PARAMS):
        evidence['conclusions'].append('P7_R01_NEGATIVE_RESULT')
    args.output.mkdir(parents=True, exist_ok=True)
    # Keep economic details once, without duplicating the existing SQLite ledgers.
    (args.output/'evidence.json').write_text(json.dumps(evidence, indent=2, allow_nan=False), encoding='utf-8')
    lines = ['| Strategy | Orders/fills | Fees USDT | Return % | Max DD % | Hourly vol % | Descriptive Sharpe | Turnover | Time invested % |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for name, result in evidence['paper'].items():
        sharpe = result['annualized_sharpe_descriptive']
        lines.append(f"| {name} | {result['orders']}/{result['fills']} | {float(result['total_fees']):.6f} | {100*result['paper_return']:.6f} | {100*result['max_drawdown']:.6f} | {100*result['hourly_volatility']:.6f} | {sharpe if sharpe is not None else 'N/A'} | {result['turnover_initial_equity']:.6f} | {100*result['time_in_market']:.2f} |")
    (args.output/'paper-table.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
