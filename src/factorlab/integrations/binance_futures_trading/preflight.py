"""Demo-only GET preflight. No execution authorization or network writer is loaded."""
import json
from datetime import datetime, timezone
from decimal import Decimal

from factorlab.errors import DataQualityError
from .protocol import Credentials, Environment, ExchangeError, FuturesClient, ReadOnlyNetworkTransport


OFFICIAL_SOURCE = ('https://github.com/binance/binance-connector-python/blob/'
                   'f75b4a52c5aebf52b1c6ad671b0ce53b96f8e179/clients/'
                   'derivatives_trading_usds_futures/src/'
                   'binance_sdk_derivatives_trading_usds_futures/rest_api/')
SOURCES = {
    'account_v2': OFFICIAL_SOURCE + 'models/account_information_v2_response.py',
    'account_v3': OFFICIAL_SOURCE + 'models/account_information_v3_response.py',
    'quantitative': OFFICIAL_SOURCE + 'models/futures_trading_quantitative_rules_indicators_response_indicators.py',
    'lock': OFFICIAL_SOURCE + 'models/futures_trading_quantitative_rules_indicators_response_indicators_account_inner.py',
    'endpoints': OFFICIAL_SOURCE + 'api/account_api.py',
}


def account_can_trade(*accounts):
    flags = [account['canTrade'] for account in accounts
             if isinstance(account, dict) and 'canTrade' in account]
    if not flags or any(type(flag) is not bool for flag in flags):
        return None
    return flags[0] if all(flag is flags[0] for flag in flags) else None


def quantitative_evidence(status):
    indicators = status.get('indicators') if isinstance(status, dict) else None
    evidence = {'indicators_present': isinstance(indicators, dict), 'scopes': {}}
    observations = []
    valid = isinstance(indicators, dict)
    for scope in ('NVDAUSDT', 'ACCOUNT'):
        rows = indicators.get(scope) if isinstance(indicators, dict) else None
        fields = {'present': isinstance(rows, list), 'nonempty': bool(rows) if isinstance(rows, list) else False}
        flags = {}
        complete_rows = isinstance(rows, list) and bool(rows)
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                complete_rows = False
                continue
            name, locked = row.get('indicator'), row.get('isLocked')
            known = {'TMV'} if scope == 'ACCOUNT' else {'UFR', 'IFER', 'GCR', 'DR'}
            if not isinstance(name, str) or name not in known or type(locked) is not bool:
                complete_rows = False
                continue
            if name in flags:
                complete_rows = False
            flags[name] = locked
        fields['explicit_boolean_rows'] = complete_rows
        fields['observed_locked'] = any(flags.values()) if complete_rows else None
        evidence['scopes'][scope] = fields
        valid = valid and complete_rows
        observations.extend(flags.values())
    # TODO: Establish an exhaustive official indicator contract before certifying
    # all-clear. Optional SDK fields do not define absent indicators as unlocked.
    evidence['unlocked_coverage_established'] = False
    conclusion = False if valid and any(observations) else None
    return conclusion, evidence


def verdict(value):
    return 'VERIFIED_TRUE' if value is True else 'VERIFIED_FALSE' if value is False else 'UNKNOWN'


def inspect_eligibility(client):
    if client.environment is not Environment.DEMO or type(client.transport) is not ReadOnlyNetworkTransport:
        raise ValueError('Demo read-only client required')
    result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'environment': 'DEMO',
              'symbol': 'NVDAUSDT', 'network_writes': 0, 'endpoints': [], 'conditions': {},
              'sources': SOURCES, 'execution_admission': 'BLOCKED', 'formal_admission': 'BLOCKED',
              'production_writes': 'BLOCKED'}
    values = {}
    for path in ('/fapi/v1/time', '/fapi/v2/account', '/fapi/v1/apiTradingStatus'):
        row = {'endpoint': path}
        try:
            if path.endswith('/time'):
                client.sync_time()
            else:
                values[path] = client.request('GET', path, {}, signed=True)
            row['read_ok'] = True
        except (ExchangeError, DataQualityError, OSError, TimeoutError):
            row['read_ok'] = False
        result['endpoints'].append(row)
        if path.endswith('/time') and not row['read_ok']:
            break
    account = values.get('/fapi/v2/account')
    can_trade = account_can_trade(account)
    unlocked, locks = quantitative_evidence(values.get('/fapi/v1/apiTradingStatus'))
    result['conditions'] = {'account_can_trade': can_trade, 'quantitative_trading_unlocked': unlocked}
    result['verdicts'] = {name: verdict(value) for name, value in result['conditions'].items()}
    result['evidence'] = {
        'account_v2': {'canTrade_present': isinstance(account, dict) and 'canTrade' in account,
                       'canTrade_boolean': can_trade},
        'quantitative': locks,
    }
    return result


def inspect(client):
    if client.environment is not Environment.DEMO or type(client.transport) is not ReadOnlyNetworkTransport:
        raise ValueError('Demo read-only client required')
    result = {'observed_at': datetime.now(timezone.utc).isoformat(), 'environment': 'DEMO',
              'symbol': 'NVDAUSDT', 'network_writes': 0, 'endpoints': [], 'conditions': {}}
    values = {}
    paths = [('/fapi/v1/time', False, {}), ('/fapi/v1/exchangeInfo', False, {}),
             ('/fapi/v3/account', True, {}), ('/fapi/v2/account', True, {}),
             ('/fapi/v3/positionRisk', True, {}),
             ('/fapi/v1/positionSide/dual', True, {}),
             ('/fapi/v1/symbolConfig', True, {'symbol': 'NVDAUSDT'}),
             ('/fapi/v1/leverageBracket', True, {'symbol': 'NVDAUSDT'}),
             ('/fapi/v1/openOrders', True, {}),
             ('/fapi/v1/apiTradingStatus', True, {})]
    for path, signed, params in paths:
        row = {'endpoint': path}
        try:
            if path.endswith('/time'):
                client.sync_time()
            else:
                values[path] = client.request('GET', path, params, signed=signed)
            row.update(result='READ_OK', http_status=200)
        except ExchangeError as error:
            row.update(result=error.category, http_status=error.status, code=error.code)
        except (DataQualityError, OSError, TimeoutError):
            row.update(result='READ_FAILED')
        result['endpoints'].append(row)
    conditions = result['conditions']
    info = values.get('/fapi/v1/exchangeInfo', {})
    symbol = next((r for r in info.get('symbols', []) if r.get('symbol') == 'NVDAUSDT'), {})
    result['public_rules'] = {k: symbol.get(k) for k in
        ('symbol', 'status', 'contractType', 'pricePrecision', 'quantityPrecision', 'orderTypes', 'filters')}
    conditions['trading_symbol'] = symbol.get('status') == 'TRADING' if symbol else None
    account = values.get('/fapi/v3/account', {})
    conditions['account_can_trade'] = account_can_trade(account, values.get('/fapi/v2/account'))
    asset = next((a for a in account.get('assets', []) if a.get('asset') == 'USDT'), {})
    conditions['available_margin_known'] = asset.get('availableBalance') is not None
    conditions['available_margin_positive'] = (Decimal(asset['availableBalance']) > 0
                                                if asset.get('availableBalance') is not None else None)
    conditions['initial_margin_known'] = asset.get('initialMargin') is not None
    conditions['wallet_and_pnl_known'] = all(asset.get(k) is not None for k in ('walletBalance', 'unrealizedProfit'))
    mode = values.get('/fapi/v1/positionSide/dual', {}).get('dualSidePosition')
    conditions['one_way_mode'] = not mode if type(mode) is bool else None
    configs = values.get('/fapi/v1/symbolConfig', [])
    config = next((r for r in configs if r.get('symbol') == 'NVDAUSDT'), {})
    conditions['leverage_known'] = type(config.get('leverage')) is int and config['leverage'] > 0
    conditions['margin_mode_known'] = config.get('marginType') in {'CROSSED', 'ISOLATED'}
    brackets = values.get('/fapi/v1/leverageBracket', [])
    bracket = next((r for r in brackets if r.get('symbol') == 'NVDAUSDT'), {})
    conditions['risk_brackets_known'] = bool(bracket.get('brackets'))
    positions = account.get('positions')
    conditions['positions_known'] = isinstance(positions, list) and '/fapi/v3/positionRisk' in values
    conditions['no_existing_positions'] = (all(Decimal(p['positionAmt']) == 0 for p in positions)
                                            if isinstance(positions, list) else None)
    orders = values.get('/fapi/v1/openOrders')
    conditions['orders_known'] = isinstance(orders, list)
    conditions['no_existing_orders'] = not orders if isinstance(orders, list) else None
    status = values.get('/fapi/v1/apiTradingStatus', {})
    conditions['quantitative_trading_unlocked'], _ = quantitative_evidence(status)
    result['unknown'] = [k for k, v in conditions.items() if v is None or k.endswith('_known') and not v]
    result['plan'] = 'NOT_AUTHORIZED_BOUNDARIES_UNSET'
    result['execution_admission'] = 'BLOCKED'
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--eligibility-only', action='store_true')
    args = parser.parse_args()
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.title('FactorLab — Demo GET-only preflight')
    tk.Label(root, text='FactorLab-Demo only — GET requests only\nNo orders, cancellation or credential persistence.', padx=20, pady=15).pack()
    fields = []
    for label in ('Demo API Key', 'Demo Secret Key'):
        tk.Label(root, text=label).pack()
        entry = tk.Entry(root, show='*', width=70, exportselection=False)
        entry.pack(padx=20, pady=6)
        fields.append(entry)
    credentials = []
    def submit():
        pair = [e.get().strip() for e in fields]
        if not all(pair):
            messagebox.showerror('Missing credentials', 'Enter existing Demo key and secret.')
            return
        credentials.append(Credentials(*pair, Environment.DEMO))
        for e in fields:
            e.delete(0, tk.END)
        root.destroy()
    tk.Button(root, text='Run Demo GET-only checks', command=submit).pack(pady=15)
    root.mainloop()
    if not credentials:
        print('{"result":"CANCELLED"}')
        return
    client = FuturesClient(credentials.pop(), environment=Environment.DEMO,
        transport=ReadOnlyNetworkTransport(environment=Environment.DEMO, permit_account_read=True))
    try:
        result = inspect_eligibility(client) if args.eligibility_only else inspect(client)
    finally:
        client.credentials = None
    print(json.dumps(result, ensure_ascii=True))


if __name__ == '__main__':
    main()
