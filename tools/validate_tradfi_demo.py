"""Capture public TradFi listing and QQQ market evidence without account writes."""
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1] / 'docs/validation/v69-f01'
TARGETS = ('AAPL', 'SNDK', 'BA', 'C', 'JPM', 'GE', 'TQQQ', 'QQQ')

def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    def save(name, value):
        (ROOT / (name + '.json')).write_text(json.dumps(value, indent=2) + '\n', encoding='utf-8')
    def get(host, path):
        with urlopen(host + path, timeout=30) as response:
            return json.load(response)
    snapshots = {name: json.loads((ROOT / (name + '_exchangeInfo.json')).read_text()) for name in ('mainnet', 'demo')}
    equity = lambda row: row.get('underlyingType') == 'EQUITY' and 'TradFi' in row.get('underlyingSubType', [])
    listings = [row for row in snapshots['demo']['symbols'] if equity(row)]
    save('demo_equity_tradfi', listings)
    markets = {}
    for name, path in {'book':'/fapi/v1/ticker/bookTicker?symbol=QQQUSDT', 'depth':'/fapi/v1/depth?symbol=QQQUSDT&limit=5', 'klines':'/fapi/v1/klines?symbol=QQQUSDT&interval=1h&limit=100', 'mark':'/fapi/v1/premiumIndex?symbol=QQQUSDT', 'funding':'/fapi/v1/fundingRate?symbol=QQQUSDT&limit=10'}.items():
        try:
            markets[name] = get('https://demo-fapi.binance.com', path)
        except Exception as exc:
            markets[name] = {'error_type':type(exc).__name__, 'message':str(exc)}
    save('qqq_market_evidence', {'observed_at':datetime.now(timezone.utc).isoformat(), **markets})
    mapping=[]
    for ticker in TARGETS:
        rows={name:next((r for r in snap['symbols'] if r['symbol']==ticker+'USDT' and equity(r)),None) for name,snap in snapshots.items()}
        status='DEMO_LISTED_NOT_EXECUTED' if rows['demo'] else 'MAINNET_ONLY' if rows['mainnet'] else 'NOT_LISTED'
        mapping.append(dict(stock=ticker, candidate_contract=ticker+'USDT' if any(rows.values()) else None, identity_basis='exchangeInfo EQUITY/TradFi metadata; issuer-level identity not independently verified', mainnet=rows['mainnet'], demo=rows['demo'], classification=status, reason='QQQ execution unsupported by existing NVDA-only admission' if rows['demo'] else 'No corresponding EQUITY/TradFi contract in Demo snapshot', rejected_collision='CUSDT is COIN, not Citigroup' if ticker=='C' else None))
    save('instrument_mapping', mapping)
    save('validation', dict(observed_at=datetime.now(timezone.utc).isoformat(), complete=False, execution_verified=False, orders_submitted=0, fees=None, account_equity_reconciliation=None, restart_reconciliation=None, leverage_brackets=None, issuer_identity_verified=False, classifications={r['stock']:r['classification'] for r in mapping}, blockers=['Existing observe_demo_rules rejects all symbols except NVDAUSDT; DemoSession and strategy runtime are also NVDA-specific', 'Azure account ownership coordination not established'], market_coverage={k:'error' not in v if isinstance(v,dict) else bool(v) for k,v in markets.items()}))
    print('Demo EQUITY/TradFi:', ', '.join(r['symbol'] for r in listings))
    print('Market evidence:', ', '.join(markets))

if __name__ == '__main__':
    main()
