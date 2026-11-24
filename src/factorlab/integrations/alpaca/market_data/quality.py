"""Observed coverage and adjustment diagnostics, not research certification."""
import math
import pandas as pd


def audit_bars(rows, sessions, *, symbol, feed, adjustment, timeframe='1Day'):
    selected = [r for r in rows if r['symbol'] == symbol]
    dates, timestamps, invalid = [], [], []
    for row in selected:
        stamp = pd.Timestamp(row['t'])
        day = stamp.tz_convert('America/New_York').tz_localize(None).normalize()
        dates.append(day)
        timestamps.append(stamp)
        values = [float(row[k]) for k in ('o', 'h', 'l', 'c', 'v')]
        o, h, l, c, v = values
        if (not all(math.isfinite(x) for x in values) or min(o, h, l, c) <= 0
                or v < 0 or h < max(o, l, c) or l > min(o, h, c)):
            invalid.append(row['t'])
    expected = pd.DatetimeIndex(sessions.index)
    observed = pd.DatetimeIndex(dates).unique()
    missing = expected.difference(observed)
    outside = observed.difference(expected)
    extended = []
    if timeframe != '1Day':
        for stamp, day in zip(timestamps, dates):
            if day in sessions.index and not sessions.loc[day, 'open'] <= stamp < sessions.loc[day, 'close']:
                extended.append(stamp.isoformat())
    return {'symbol': symbol, 'feed': feed, 'adjustment': adjustment, 'timeframe': timeframe,
            'first_valid_date': str(min(dates).date()) if dates else None,
            'last_valid_date': str(max(dates).date()) if dates else None,
            'rows': len(selected), 'sessions': len(observed), 'expected_sessions': len(expected),
            'missing_sessions': [str(x.date()) for x in missing],
            'outside_calendar': [str(x.date()) for x in outside],
            'duplicate_records': len(timestamps) - len(set(timestamps)),
            'invalid_ohlcv': invalid, 'extended_hour_rows': len(extended),
            'volume_scope': 'IEX_SINGLE_EXCHANGE' if feed == 'iex' else 'SIP_CONSOLIDATED' if feed == 'sip' else feed,
            'status': 'PASS' if not (len(missing) or len(outside) or invalid or len(timestamps) != len(set(timestamps))) else 'GAPS_OR_INVALID'}


def regular_session_aggregates(rows, sessions):
    """Aggregate only observed minute bars inside actual scheduled core hours."""
    groups = {}
    for row in rows:
        event = pd.Timestamp(row['t'])
        day = event.tz_convert('America/New_York').tz_localize(None).normalize()
        if day in sessions.index and sessions.loc[day, 'open'] <= event < sessions.loc[day, 'close']:
            groups.setdefault((row['symbol'], day), []).append(row)
    result = []
    for (symbol, day), values in sorted(groups.items()):
        values.sort(key=lambda row: pd.Timestamp(row['t']))
        result.append({'symbol': symbol, 'session': str(day.date()), 'scope': 'OBSERVED_RTH_MINUTES',
                       'o': values[0]['o'], 'h': max(x['h'] for x in values),
                       'l': min(x['l'] for x in values), 'c': values[-1]['c'],
                       'v': sum(x['v'] for x in values), 'observed_minutes': len(values),
                       'scheduled_minutes': int((sessions.loc[day, 'close'] - sessions.loc[day, 'open']).total_seconds() / 60),
                       'completeness': 'NOT_INFERRED_FROM_MINUTE_COUNT'})
    return result


def adjustment_relationship(raw, adjusted):
    left = {(r['symbol'], r['t']): r for r in raw}
    right = {(r['symbol'], r['t']): r for r in adjusted}
    records = []
    for key in sorted(left.keys() & right.keys()):
        a, b = left[key], right[key]
        records.append({'symbol': key[0], 'event_at': key[1],
                        **{k + '_ratio': float(b[k]) / float(a[k]) for k in ('o', 'h', 'l', 'c')},
                        'volume_ratio': float(b['v']) / float(a['v']) if a['v'] else None})
    return {'matched': len(records), 'raw_only': len(left.keys() - right.keys()),
            'adjusted_only': len(right.keys() - left.keys()), 'ratios': records,
            'pit': 'UNVERIFIED', 'hfq': 'UNVERIFIED', 'v69_equivalence': 'UNCONFIRMED_ORIGINAL_SOURCE_REQUIRED'}
