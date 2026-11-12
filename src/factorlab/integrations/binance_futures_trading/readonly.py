"""Explicit two-environment GET verification; private payloads never leave memory."""
import argparse
from datetime import datetime, timezone
import json

from factorlab.errors import DataQualityError
from .protocol import Credentials, Environment, ExchangeError, FuturesClient, ReadOnlyNetworkTransport


def verify(credentials):
    if set(credentials) != set(Environment):
        raise ValueError('both isolated credential sets required')
    if credentials[Environment.PRODUCTION].key == credentials[Environment.DEMO].key:
        raise ValueError('production and Demo API keys must differ')
    report = {'observed_at': datetime.now(timezone.utc).isoformat(),
              'safety': 'NO LIVE MONEY / PIT_UNVERIFIED / HFQ_UNVERIFIED / FORMAL_ADMISSION_BLOCKED',
              'capabilities': [], 'demo_symbols': []}
    for environment in Environment:
        client = FuturesClient(credentials[environment], environment=environment,
            transport=ReadOnlyNetworkTransport(environment=environment, permit_account_read=True))
        paths = [('/fapi/v1/time', False), ('/fapi/v1/exchangeInfo', False),
                 ('/fapi/v3/account', True), ('/fapi/v3/balance', True),
                 ('/fapi/v3/positionRisk', True), ('/fapi/v1/openOrders', True)]
        for path, signed in paths:
            row = dict(environment=environment.name, host=environment.host, endpoint=path)
            try:
                if path == '/fapi/v1/time':
                    client.sync_time()
                    payload = None
                else:
                    payload = client.request('GET', path, signed=signed)
                if path == '/fapi/v1/exchangeInfo':
                    if not isinstance(payload, dict) or not isinstance(payload.get('symbols'), list):
                        raise DataQualityError('invalid exchangeInfo')
                    if environment is Environment.DEMO:
                        report['demo_symbols'] = [
                            {'symbol': item['symbol'], 'status': item.get('status'),
                             'contractType': item.get('contractType')}
                            for item in payload['symbols']]
                        report['NVDAUSDT'] = next((s for s in report['demo_symbols']
                                                   if s['symbol'] == 'NVDAUSDT'), None)
                row.update(result='READ_OK', http_status=200)
                del payload
            except ExchangeError as error:
                row.update(result=error.category, http_status=error.status, exchange_code=error.code)
            except (DataQualityError, OSError, TimeoutError):
                row.update(result='READ_FAILED')
            report['capabilities'].append(row)
    from factorlab.integrations.binance_equity.client import Client, MarketDataFailure

    class DiscardReceipt:
        def raw(self, *args, **kwargs):
            return None

    row = dict(environment='PRODUCTION', host='https://api.binance.com',
               endpoint='/sapi/v1/equity/market/exchangeInfo')
    try:
        client = Client(network=True, timeout=10, credentials=credentials[Environment.PRODUCTION])
        payload, _, _ = client._get('exchangeInfo', 'NVDA', DiscardReceipt())
        if not isinstance(payload, (dict, list)):
            raise DataQualityError('invalid Stocks rules response')
        del payload
        row.update(result='READ_OK', http_status=200)
    except MarketDataFailure as error:
        row.update(result=error.reason, http_status=error.status, exchange_code=error.code)
    except (DataQualityError, OSError, TimeoutError):
        row.update(result='READ_FAILED')
    report['capabilities'].append(row)
    return report


def credential_dialog():
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.title('FactorLab — Binance read-only verification')
    root.resizable(False, False)
    tk.Label(root, text='NO LIVE MONEY — GET requests only\nKeys remain in process memory; no credential files.',
             padx=20, pady=15).pack()
    entries = {}
    for environment in Environment:
        group = tk.LabelFrame(root, text=environment.account_id, padx=15, pady=10)
        group.pack(fill='x', padx=20, pady=8)
        tk.Label(group, text=environment.host).grid(row=0, columnspan=2, sticky='w')
        fields = []
        for index, label in enumerate(('API Key', 'Secret Key'), 1):
            tk.Label(group, text=label).grid(row=index, column=0, sticky='w')
            entry = tk.Entry(group, show='*', width=65, exportselection=False)
            entry.grid(row=index, column=1, padx=8, pady=5)
            fields.append(entry)
        entries[environment] = fields
    result = {}

    def submit():
        values = {env: tuple(e.get().strip() for e in fields) for env, fields in entries.items()}
        if any(not value for pair in values.values() for value in pair):
            messagebox.showerror('Missing credentials', 'Enter API Key and Secret Key for both environments.')
            return
        if values[Environment.PRODUCTION][0] == values[Environment.DEMO][0]:
            messagebox.showerror('Environment isolation', 'Production and Demo must use different API keys.')
            return
        result.update({env: Credentials(*pair, environment=env) for env, pair in values.items()})
        for fields in entries.values():
            for entry in fields:
                entry.delete(0, tk.END)
        root.destroy()

    tk.Button(root, text='Run read-only verification', command=submit, padx=15, pady=6).pack(pady=15)
    root.mainloop()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-dialog', action='store_true')
    parser.add_argument('--permit-account-read', action='store_true', required=True)
    args = parser.parse_args()
    credentials = credential_dialog() if args.credential_dialog else {
        env: Credentials.from_env(env) for env in Environment}
    if not credentials:
        print('{"result":"CANCELLED"}')
        return
    report = verify(credentials)
    credentials.clear()
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    main()
