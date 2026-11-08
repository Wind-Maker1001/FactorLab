"""Calculate observed-member-date runs; no strategy returns or identity changes."""
import argparse
import json
import zipfile
from pathlib import Path
import pandas as pd


def runs(frame, calendar):
    positions = {day: i for i, day in enumerate(calendar)}
    output = []
    for code, group in frame.groupby('instrument'):
        dates = sorted(group.date.unique())
        first = previous = dates[0]
        count = 1
        for day in dates[1:]:
            if positions[day] == positions[previous] + 1:
                count += 1
            else:
                output.append({'instrument': code, 'start': first, 'end': previous, 'member_dates': count})
                first, count = day, 1
            previous = day
        output.append({'instrument': code, 'start': first, 'end': previous, 'member_dates': count})
    return output


def run(args):
    root = Path(args.private_root)
    with zipfile.ZipFile(args.membership_zip) as z:
        members = pd.read_csv(z.open('members_daily.csv'), usecols=['date'])
    calendar = sorted(members.loc[members.date.between('2021-09-13', '2026-09-11'), 'date'].unique())
    missing = pd.read_csv(root / 'missing_keys.csv')
    nulls = pd.read_csv(root / 'null_fields.csv')
    ohlc = nulls.loc[nulls.open.isna()]
    intervals = {'calendar_basis': 'OBSERVED_MEMBERSHIP_DATES_NOT_INDEPENDENT_EXCHANGE_CALENDAR',
                 'missing': runs(missing, calendar), 'ohlc': runs(ohlc, calendar)}
    (root / 'continuous_intervals.json').write_text(json.dumps(intervals, indent=2), encoding='utf-8')
    affected = pd.concat([missing, ohlc]).groupby('date').size()
    result = {'missing_key_ratio': len(missing) / 605500, 'ohlc_null_ratio': len(ohlc) / 605500,
              'complete_ohlc_member_days': 605500 - len(missing) - len(ohlc),
              'dates_with_ohlc_null': int(ohlc.date.nunique()), 'securities_with_ohlc_null': int(ohlc.instrument.nunique()),
              'dates_with_any_missing_or_ohlc_null': len(affected), 'maximum_daily_unavailable': int(affected.max()),
              'minimum_daily_complete_ohlc': 500 - int(affected.max()), 'observed_member_dates': len(calendar),
              'missing_runs': intervals['missing'], 'ohlc_run_count': len(intervals['ohlc'])}
    (root / 'sensitivity.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    p = argparse.ArgumentParser(__doc__)
    p.add_argument('--private-root', required=True)
    p.add_argument('--membership-zip', required=True)
    run(p.parse_args())
