"""Local four-arm Paper comparison; no network or credentials."""
import argparse
from decimal import Decimal
import json
from pathlib import Path

import pandas as pd

from factorlab.experiments.grid_paper import deterministic_bars, run_comparison
from factorlab.strategies.grid import GridConfig
from factorlab.strategies.grid_execution import GridRules
from factorlab.trading.contracts import Market


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    data = parser.add_mutually_exclusive_group()
    data.add_argument('--bars', type=Path, help='Completed daily OHLC CSV; omit for fixed fixture')
    data.add_argument('--snapshot', type=Path, help='Existing daily Binance history snapshot directory')
    parser.add_argument('--config', type=Path, help='GridConfig JSON, fixed before evaluation')
    parser.add_argument('--rules', type=Path, help='GridRules JSON with explicit Decimal strings')
    parser.add_argument('--capital', type=Decimal, default=Decimal(1000))
    parser.add_argument('--market', type=Market, choices=list(Market), default=Market.BINANCE_EQUITY)
    parser.add_argument('--instrument-id', default='GRIDUSD')
    parser.add_argument('--fee-rate', type=Decimal, default=Decimal('.001'))
    parser.add_argument('--slippage', type=Decimal, default=Decimal('.0005'))
    parser.add_argument('--participation', type=Decimal, default=Decimal('.25'))
    parser.add_argument('--interval', choices=['1d', '1h'], default='1d')
    parser.add_argument('--start')
    parser.add_argument('--end')
    args = parser.parse_args()
    config = (GridConfig(**json.loads(args.config.read_text(encoding='utf-8'))) if args.config else
        GridConfig(lower=80, upper=120, levels=4, initial_cash=1000,
            initial_inventory=0, initial_price=100, quantity=2, atr_window=2,
            max_inventory=100, max_cash_committed=1000, max_drawdown=1,
            fee_bps=10, slippage_bps=5, breakout='pause'))
    rules = (GridRules(**{key: Decimal(value) for key, value in
                          json.loads(args.rules.read_text(encoding='utf-8')).items()}) if args.rules else
        GridRules(Decimal(1), Decimal('.1'), Decimal('.1'), Decimal('.1'), Decimal(1)))
    bars = pd.read_csv(args.bars).to_dict('records') if args.bars else deterministic_bars()
    data_identity = 'FIXED_DAILY_GRID_FIXTURE_V1' if args.bars is None else str(args.bars)
    if args.snapshot:
        manifest = json.loads((args.snapshot/'manifest.json').read_text(encoding='utf-8'))
        if args.interval == '1h':
            from factorlab.experiments.grid_empirical import load_hourly
            bars, data_identity = load_hourly(args.snapshot)
        else:
            if manifest['interval'] != '1d' or not manifest['integrity']['complete']:
                parser.error('a complete daily history snapshot is required')
            frame = pd.read_parquet(args.snapshot/manifest['tables']['bars']['path'],
                                    columns=['open_time', 'close', 'volume', 'high', 'low'])
            bars = [dict(id=f'snapshot-{i}', time=str(row.open_time.date()),
                price=float(row.close), volume=int(Decimal(str(row.volume))), completed_bar=True,
                high=float(row.high), low=float(row.low), close=float(row.close))
                for i, row in enumerate(frame.itertuples())]
            data_identity = dict(data_version=manifest['data_version'],
                instrument_id=manifest['contract']['instrument_id'],
                liquidity='observed daily volume floored to integer units; synthetic participation',
                costs='conditional proportional fees and slippage; funding not simulated')
    run_comparison(args.output, bars=bars, config=config, rules=rules,
        capital=args.capital, fee_rate=args.fee_rate, slippage=args.slippage,
        participation=args.participation, start=args.start, end=args.end,
        market=args.market, instrument_id=args.instrument_id, data_identity=data_identity,
        interval=args.interval)
    print(args.output)


if __name__ == '__main__':
    main()
