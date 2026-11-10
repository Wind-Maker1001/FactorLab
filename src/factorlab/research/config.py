"""Strict portable research specifications; local bindings are separate."""
import json
import math
from pathlib import Path
import re

import pandas as pd

from factorlab.backtest.fees import FeeSchedule
from factorlab.strategies.dual_ma import DualMAConfig
from factorlab.experiments.tracker import SAFETY

EXECUTION = dict(frequency='daily', signal_timing='close', execution_timing='next_open',
                 long_only=True, financial_admission='FORMAL_ADMISSION_BLOCKED', network='NO_AUTOMATIC_ACCESS')
BOUNDARY = dict(state='RESEARCH_ONLY', safety=SAFETY, formal_admission=False)


def object_fields(value, names, label):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError(f"{label} fields must be {names}")


def digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('SHA256 requires 64 lowercase hex characters')


def number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(label + ' requires a finite number')


def text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + ' requires a nonempty string')


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('dates require YYYY-MM-DD')
    return pd.Timestamp(value)


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate JSON field: ' + key)
            result[key] = value
        return result
    return json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique,
                      parse_constant=lambda s: (_ for _ in ()).throw(ValueError('invalid JSON number: ' + s)))


def validate_config(config):
    object_fields(config, 'schema_version task data fees research execution boundary', 'research configuration')
    if (json.dumps(config['execution'], sort_keys=True) != json.dumps(EXECUTION, sort_keys=True)
            or json.dumps(config['boundary'], sort_keys=True) != json.dumps(BOUNDARY, sort_keys=True)):
        raise ValueError('only the explicit offline research-only execution and admission contract is supported')
    if type(config['schema_version']) is not int or config['schema_version'] != 1:
        raise ValueError('unsupported research schema version')
    if config['task'] not in ('custom_daily', 'p6_r01'):
        raise ValueError('unknown research task')
    data = config['data']
    object_fields(data, 'id manifest_sha256 source_kind', 'data')
    text(data['id'], 'data.id')
    if ':' in data['id'] or '/' in data['id'] or '\\' in data['id']:
        raise ValueError('data.id must be logical, not a path')
    digest(data['manifest_sha256'])
    if data['source_kind'] not in ('SYNTHETIC', 'REAL_CANDIDATE'):
        raise ValueError('source_kind is not financial admission')
    object_fields(config['fees'], 'commission_bps minimum_commission_cny slippage_bps transfer_fee_bps', 'fees')
    for key, value in config['fees'].items():
        number(value, key)
    FeeSchedule.a_share_default(**config['fees'])
    research = config['research']
    if config['task'] == 'custom_daily':
        object_fields(research, 'symbol start end warmup_start initial_cash max_weight board_lot strategy', 'custom research')
        strategy = research['strategy']
        object_fields(strategy, 'class parameters sha256', 'strategy')
        if not isinstance(strategy['class'], str) or not strategy['class'].isidentifier():
            raise ValueError('strategy class must be an identifier')
        if not isinstance(strategy['parameters'], dict):
            raise ValueError('strategy parameters must be an object')
        digest(strategy['sha256'])
        common = {k: research[k] for k in ('symbol', 'start', 'end', 'initial_cash', 'max_weight', 'board_lot')}
        _window(research)
        _common(common)
    else:
        object_fields(research, 'windows common strategies signal_price_column', 'P6 research')
        object_fields(research['common'], 'initial_cash max_weight board_lot', 'account common')
        if research['signal_price_column'] != 'raw_close':
            raise ValueError('P6 research requires raw_close')
        if not isinstance(research['windows'], list) or not research['windows']:
            raise ValueError('explicit nonempty research windows required')
        keys = []
        for w in research['windows']:
            object_fields(w, 'symbol industry year start end warmup_start query_start query_end', 'window')
            _window(w)
            text(w['industry'], 'industry')
            if type(w['year']) is not int or day(w['start']).year != w['year'] or day(w['end']).year != w['year']:
                raise ValueError('window must lie within its account year')
            if not day(w['warmup_start']) <= day(w['query_start']) < day(w['start']) <= day(w['query_end']) <= day(w['end']):
                raise ValueError('query must cover warmup and research within declared scope')
            _common(dict(research['common'], symbol=w['symbol'], start=w['start'], end=w['end']))
            keys.append((w['symbol'], w['year']))
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate symbol-year accounts')
        _strategies(research['strategies'])
    return config


def _window(w):
    if not day(w['warmup_start']) < day(w['start']) <= day(w['end']):
        raise ValueError('ordered warmup/start/end required')
    if not isinstance(w['symbol'], str) or not re.fullmatch(r'\d{6}\.(SH|SZ|BJ)', w['symbol']):
        raise ValueError('canonical security symbol required')


def _common(common):
    for name in ('initial_cash', 'max_weight'):
        number(common[name], name)
    if type(common['board_lot']) is not int:
        raise ValueError('board_lot must be integer')
    DualMAConfig(**common)


def _strategies(strategies):
    from factorlab.strategies.technical import BollingerConfig, RSIConfig, MACDConfig, CTAConfig
    object_fields(strategies, 'dual_ma bollinger rsi macd cta buy_hold', 'strategies')
    names = {
        'dual_ma': (DualMAConfig, 'short_window long_window'),
        'bollinger': (BollingerConfig, 'window deviations mode'),
        'rsi': (RSIConfig, 'window oversold overbought exit_rule'),
        'macd': (MACDConfig, 'fast slow signal_window zero_filter'),
        'cta': (CTAConfig, 'entry_window exit_window atr_window trend_window atr_multiple risk_fraction'),
    }
    for name, (cls, keys) in names.items():
        object_fields(strategies[name], keys, name)
        cls(**strategies[name])
    object_fields(strategies['buy_hold'], '', 'buy_hold')


def load_config(path):
    return validate_config(read_json(path))


def load_runtime(path, task):
    runtime = read_json(path)
    names = 'research_root source_root' + (' strategy_file' if task == 'custom_daily' else '')
    object_fields(runtime, names, 'local runtime')
    for name, value in runtime.items():
        text(value, name)
        if not Path(value).is_absolute():
            raise ValueError(name + ' must be an absolute local path')
    return runtime
