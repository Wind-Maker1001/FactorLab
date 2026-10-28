"""Deterministic US software fixture, deliberately unrelated to real returns."""
import numpy as np
import pandas as pd
from .calendar import us_calendar
from .identity import IdentityRegistry, DynamicUniverse
from .adapters import USEquityAdapter, ActionLedger, DatedFees, FeePeriod
from .contracts import session_date


def build_us(start='2023-09-01', end='2025-02-28', count=72, seed=42):
    calendar = us_calendar(start, pd.Timestamp(end) + pd.Timedelta(days=15))
    dates = calendar.sessions(start, end).index
    aliases, rows, actions = [], [], []
    def action(i, kind, day, ratio=1.0, cash=0.0, target=None, pay=None):
        actions.append(dict(event_id='fixture:' + str(i) + ':' + kind, instrument_id='fixture:US:' + str(i),
            kind=kind, effective_date=day, available_at=pd.Timestamp(day, tz='UTC') - pd.Timedelta(days=7),
            ratio=ratio, cash_per_share=cash, target_id=target, pay_date=pay,
            evidence='DETERMINISTIC_SYNTHETIC_EVENT:' + kind))
    action(0, 'split', '2024-06-03', 2)
    action(1, 'reverse_split', '2024-07-01', 0.2)
    action(2, 'cash_dividend', '2024-06-14', cash=0.2, pay='2024-06-28')
    action(3, 'symbol_change', '2024-06-10')
    action(4, 'delisting', '2024-08-01', cash=12.5)
    action(5, 'merger', '2024-08-12', ratio=0.7, cash=2.5, target='fixture:US:6')
    rng = np.random.default_rng(seed)
    for i in range(count):
        iid, symbol = 'fixture:US:' + str(i), 'S' + str(i)
        listed = pd.Timestamp('2024-04-01') if i == count - 1 else pd.Timestamp('2010-01-04')
        delisted = pd.Timestamp('2024-07-31') if i == 4 else (pd.Timestamp('2024-08-09') if i == 5 else pd.NaT)
        base = dict(instrument_id=iid, market='US', exchange='XNYS' if i % 2 else 'XNAS', asset_class='equity',
                    currency='USD', security_type='common_stock', listing_date=listed, delisting_date=delisted,
                    source_identity='DETERMINISTIC_LISTING_EPISODE:' + str(i), evidence='SYNTHETIC_IDENTITY',
                    available_at=listed.tz_localize('UTC') - pd.Timedelta(days=7))
        if i == 3:
            aliases.extend([{**base, 'symbol': symbol, 'valid_from': listed, 'valid_to': pd.Timestamp('2024-06-10')},
                            {**base, 'symbol': 'RENAMED', 'valid_from': pd.Timestamp('2024-06-10'), 'valid_to': pd.NaT}])
        else:
            aliases.append({**base, 'symbol': symbol, 'valid_from': listed, 'valid_to': pd.NaT})
        adjusted = (15 + i * 0.4) * np.exp(np.cumsum(rng.normal(0.0001, 0.006, len(dates))))
        split = np.ones(len(dates))
        if i == 0:
            split[dates >= '2024-06-03'] = 2
        if i == 1:
            split[dates >= '2024-07-01'] = 0.2
        if i == 2:
            adjusted[dates >= '2024-06-14'] -= 0.2
        for j, day in enumerate(dates):
            if day < listed or (pd.notna(delisted) and day > delisted):
                continue
            raw = adjusted[j] / split[j]
            raw_open = raw * (1 + 0.001 * np.sin(j / 4 + i))
            halted = i == 7 and pd.Timestamp('2024-06-17') <= day <= pd.Timestamp('2024-06-18')
            volume = 0 if halted else 1e6 + 1000 * j + 500 * i
            rows.append(dict(date=day, instrument_id=iid, raw_open=raw_open, raw_close=raw,
                adj_open=raw_open * split[j], adj_close=adjusted[j], volume=volume, amount=volume * raw,
                turnover=0 if halted else 0.01 + 0.002 * np.sin(j / 6 + i), halted=halted,
                available_at=calendar.at(day, 'close'), is_st=False))
    registry = IdentityRegistry(pd.DataFrame(aliases))
    universe = DynamicUniverse(registry, 'US', 'ALL_US_COMMON')
    events = pd.DataFrame(actions, columns=ActionLedger.COLUMNS)
    ledger = ActionLedger(events, coverage_start=start, coverage_end=end,
                          instrument_ids=registry.aliases.instrument_id, version='SYNTHETIC_COMPLETE_V1')
    # Demonstrate date-effective fee selection, not actual SEC/FINRA rates.
    first, last = session_date(start), session_date(end)
    scenarios = [
        (first, min(last, pd.Timestamp('2024-05-27')),
         dict(commission_per_share=0.002, minimum=0.5, sell_bps=0.1)),
        (max(first, pd.Timestamp('2024-05-28')), last,
         dict(commission_per_share=0.001, minimum=0.2, sell_bps=0.2, sell_per_share=0.0001, sell_maximum=8)),
    ]
    fees = DatedFees([FeePeriod(lo.date().isoformat(), hi.date().isoformat(), **rates)
                      for lo, hi, rates in scenarios if lo <= hi],
                     'SYNTHETIC_FEE_SCENARIO_NOT_US_TARIFF')
    adapter = USEquityAdapter(calendar, universe, ledger, fees)
    return pd.DataFrame(rows), adapter
