"""Reprice saved P9-01 fills; never execute targets or infer counterfactual NAV."""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from factorlab.backtest.fees import FeeRule, FeeSchedule
from factorlab.dataset.identity import file_hash, json_hash

LEGACY = 'LEGACY_FIXED_STAMP_10BPS'
DATED = 'DATE_EFFECTIVE_STAMP_DUTY'
COMPONENTS = ('commission', 'stamp_duty', 'transfer_fee', 'side_fee', 'slippage', 'transaction_cost')
CONFIG = ROOT / 'configs/research/p9-c01/fee-contracts.json'
FROZEN = ROOT / 'configs/research/p9-01-oos-execution.json'
EVIDENCE = ROOT / 'docs/validation/p9-01/result.json'


def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def contracts():
    """Expose the existing fee engine to subsequent research consumers."""
    config = load_json(CONFIG)
    dated = dict(config[DATED])
    dated.pop('factory')
    return {LEGACY: FeeSchedule([FeeRule(**config[LEGACY])]),
            DATED: FeeSchedule.a_share_default(**dated)}


def contract_identities(schedules):
    identities = {}
    for name, schedule in schedules.items():
        rules = [dict(asdict(rule), effective_from=str(rule.effective_from),
                      effective_to=None if rule.effective_to is None else str(rule.effective_to))
                 for rule in schedule.rules]
        identities[name] = dict(sha256=json_hash(rules), rules=rules)
    return identities


def close(actual, expected, field):
    if not math.isfinite(float(actual)) or not math.isclose(float(actual), float(expected), rel_tol=1e-10, abs_tol=1e-7):
        raise ValueError('ledger mismatch: ' + field)


def compare_fills(trades, *, start, end):
    """Validate each saved fill and return private detail plus public aggregates."""
    schedules = contracts()
    rows = []
    for trade in trades.to_dict('records'):
        day = pd.Timestamp(trade['execution_date'])
        signal = pd.Timestamp(trade['signal_date'])
        if pd.isna(day) or pd.isna(signal) or day != day.normalize() or not (pd.Timestamp(start) <= day < pd.Timestamp(end)) or signal >= day:
            raise ValueError('invalid fill date or account window')
        if not isinstance(trade['symbol'], str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', trade['symbol']):
            raise ValueError('invalid security')
        if trade['side'] not in ('buy', 'sell'):
            raise ValueError('invalid fill side')
        shares, price, amount = (float(trade[k]) for k in ('executed_shares', 'execution_price', 'trade_notional'))
        if not all(math.isfinite(v) and v > 0 for v in (shares, price, amount)) or not shares.is_integer():
            raise ValueError('invalid fill amount')
        close(amount, shares * price, 'trade_notional')
        costs = {name: schedule.calculate(day, trade['side'], amount).to_dict()
                 for name, schedule in schedules.items()}
        for field in COMPONENTS:
            close(trade[field], costs[LEGACY][field], field)
        close(sum(float(trade[k]) for k in COMPONENTS[:-1]), trade['transaction_cost'], 'fee sum')
        rows.append(dict(execution_date=str(day.date()), symbol=trade['symbol'], side=trade['side'],
                         trade_notional=amount,
                         **{name + '_' + k: value for name, cost in costs.items() for k, value in cost.items()},
                         minimum_commission_increment=costs[LEGACY]['commission'] - amount * 3 / 10000,
                         fee_difference=costs[LEGACY]['transaction_cost'] - costs[DATED]['transaction_cost']))
    detail = pd.DataFrame(rows)
    if not rows:
        return detail, dict(status='NOT_COMPUTABLE', fills=0, fees=None, fee_difference=None,
                            post_cut_fee_difference=None)
    post = detail.execution_date.ge('2023-08-28') & detail.side.eq('sell')
    return detail, dict(status='SAME_FILL_FEE_DIFFERENCE', fills=len(rows),
        first_fill=detail.execution_date.min(), last_fill=detail.execution_date.max(),
        sell_fills=int(detail.side.eq('sell').sum()), post_cut_sell_fills=int(post.sum()),
        spans_cut=bool(detail.execution_date.min() < '2023-08-28' <= detail.execution_date.max()),
        fees={name: {k: float(detail[name + '_' + k].sum()) for k in COMPONENTS} for name in schedules},
        minimum_commission_increment=float(detail.minimum_commission_increment.sum()),
        fee_difference=float(detail.fee_difference.sum()),
        post_cut_fee_difference=float(detail.loc[post, 'fee_difference'].sum()) if post.any() else None,
        post_cut_status='SAME_FILL_FEE_DIFFERENCE' if post.any() else 'NOT_COMPUTABLE')


def verify_source(source, evidence):
    config = load_json(source / 'config.json')
    identity = load_json(source / 'inference-identity.json')
    if config != load_json(FROZEN) or json_hash(config) != evidence['identity']['config_hash']:
        raise ValueError('research configuration identity drift')
    if any(identity.get(k) != v for k, v in evidence['identity'].items()):
        raise ValueError('research identity drift')
    return config, identity


def verified_file(source, relative, evidence):
    expected = evidence['private_artifact_sha256'].get(relative)
    path = source / relative
    if expected is None or not path.is_file() or file_hash(path) != expected:
        raise ValueError('original ledger identity drift: ' + relative)
    return path


def audit_cash(trades, nav, initial_cash):
    cash = initial_cash
    for day in nav.itertuples(index=False):
        fills = trades.loc[trades.execution_date.eq(day.date)]
        for fill in fills.itertuples(index=False):
            cash += (fill.trade_notional if fill.side == 'sell' else -fill.trade_notional) - fill.transaction_cost
            close(fill.cash_after, cash, 'cash_after')
        close(day.cash, cash, 'daily cash')
        close(day.transaction_cost, fills.transaction_cost.sum(), 'daily transaction_cost')
        for side in ('buy', 'sell'):
            close(getattr(day, side + '_notional'), fills.loc[fills.side.eq(side), 'trade_notional'].sum(), side + '_notional')
    if not trades.execution_date.isin(nav.date).all():
        raise ValueError('fill missing from daily ledger')


def analyze(source, output, public_output):
    source, output, public_output = Path(source), Path(output), Path(public_output)
    evidence = load_json(EVIDENCE)
    config, identity = verify_source(source, evidence)
    accounts, private = [], []
    # The pilot overlaps fold 0 and is reported separately, never added to it.
    for account in ('pilot', 'B_factor_score-fold-0', 'B_ridge-fold-0'):
        def checked(name):
            return verified_file(source, account + '/' + name, evidence)
        trades = pd.read_parquet(checked('trades.parquet'))
        nav = pd.read_parquet(checked('nav.parquet'))
        original_coverage = dict(first_fill=str(trades.execution_date.min().date()),
                                 last_fill=str(trades.execution_date.max().date()),
                                 fills=len(trades),
                                 post_cut_sell_fills=int((trades.execution_date.ge(pd.Timestamp('2023-08-28')) & trades.side.eq('sell')).sum()))
        start, end = identity['folds'][0]['test']
        if account == 'pilot':
            end = str((nav.date.max() + pd.Timedelta(days=1)).date())
        if account != 'pilot':
            gaps = pd.read_csv(checked('valuation-gaps.csv'), parse_dates=['date'])
            end = str(gaps.date.min().date())
        if account != 'pilot':
            full_detail, full_summary = compare_fills(trades, start=start, end=identity['folds'][0]['test'][1])
            audit_cash(trades, nav, config['initial_cash'])
            full_summary.update(account=account, scope='SAVED_FILLS_ONLY_UNRELIABLE_PORTFOLIO_VALUATION',
                                original_coverage=original_coverage, ledger_audit='PASSED')
            accounts.append(full_summary)
            private.append((account + '-all-saved', full_detail))
        trades = trades.loc[trades.execution_date.lt(pd.Timestamp(end))]
        nav = nav.loc[nav.date.lt(pd.Timestamp(end))]
        detail, summary = compare_fills(trades, start=start, end=end)
        audit_cash(trades, nav, config['initial_cash'])
        summary.update(account=account, scope='PILOT_ONLY' if account == 'pilot' else 'OBSERVED_PREFIX_NOT_FULL_TEST',
                       window_start=start, window_end_exclusive=end, ledger_audit='PASSED')
        accounts.append(summary)
        private.append((account, detail))
    missing = [dict(account=model + '-fold-' + str(fold), status='NOT_COMPUTABLE', fee_difference=None)
               for fold in range(3) for model in config['models'] + config['benchmarks']
               if model != 'cash' and (fold != 0 or model not in ('B_factor_score', 'B_ridge'))]
    result = dict(status='P9_C01_COST_CONTRACT_READY', comparison='SAME_FILL_FEE_DIFFERENCE',
                  source_config_hash=identity['config_hash'], contracts=contract_identities(contracts()),
                  accounts=accounts, unavailable_accounts=missing,
                  post_valuation_gap_portfolio_return='NOT_COMPUTABLE', counterfactual_portfolio_return=None,
                  safety=config['safety'])
    output.mkdir(parents=True, exist_ok=False)
    for account, detail in private:
        detail.to_csv(output / (account + '-fill-costs.csv'), index=False)
    public_output.mkdir(parents=True, exist_ok=False)
    (public_output / 'cost-summary.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True, help='New private per-fill output directory')
    parser.add_argument('--public-output', required=True, help='New aggregate-only output directory')
    args = parser.parse_args()
    analyze(args.source, args.output, args.public_output)
