import json,sqlite3
from pathlib import Path
from types import SimpleNamespace
from factorlab.trading.storage import decode
from factorlab.integrations.binance_futures_trading.demo_fill_acceptance import receipt_mapping
import argparse
parser=argparse.ArgumentParser(description='Read-only evidence export from existing Execute journals; no exchange IO.')
parser.add_argument('--state-root',type=Path,required=True)
parser.add_argument('--mailbox',type=Path)
args=parser.parse_args()
root=args.state_root
health=json.loads((root/'cloud-health.json').read_text())
protocol=next(root.glob('*/protocol.sqlite3'))
with sqlite3.connect('file:'+str(protocol)+'?mode=ro',uri=True) as db:
 adapter=SimpleNamespace(db=db,order=lambda key:decode(json.loads(db.execute('SELECT payload FROM futures_orders WHERE id=?',(key,)).fetchone()[0])).snapshot)
 all_mapping=receipt_mapping(adapter)
 mapping=[m for m in all_mapping if m['client_order_id'].startswith('pf')]
 first_time=min((t['time'] for m in mapping for t in m['trades']),default=0)
 concurrent=[dict(client_id=m['client_order_id'],trade=t) for m in all_mapping if not m['client_order_id'].startswith('pf') for t in m['trades'] if t['time']>=first_time]
 funding=[json.loads(raw) for raw, in db.execute('SELECT raw_payload FROM futures_funding')]
with sqlite3.connect('file:'+str(protocol.parent/'controller.sqlite3')+'?mode=ro',uri=True) as db:
 state=decode(json.loads(db.execute('SELECT payload FROM snapshot WHERE id=1').fetchone()[0]))
 positions=[{'symbol':p.instrument_id,'quantity':str(p.quantity),'entry_price':str(p.entry_price),'mark_price':str(p.mark_price)} for p in state.positions.values()]
 with sqlite3.connect('file:'+str(root/'runtime.sqlite3')+'?mode=ro',uri=True) as runtime:
  dispatches=[{'campaign':c,'method':m,'client_id':i,'parameters':json.loads(p),'receipt':json.loads(r) if r else None} for c,m,i,p,r in runtime.execute("SELECT * FROM dispatches WHERE campaign LIKE 'portfolio-campaign:%'")]
 mailbox=[]
 if args.mailbox and args.mailbox.exists():
  with sqlite3.connect('file:'+str(args.mailbox)+'?mode=ro',uri=True) as tasks:
   mailbox=[dict(task_id=t,recorded_at=a,status=s,detail=json.loads(d)) for t,a,s,d in tasks.execute('SELECT task_id,recorded_at,status,detail FROM portfolio_receipts ORDER BY sequence')]
 print(json.dumps({'mailbox':mailbox,'source':'EXISTING_EXECUTE_OWNER_JOURNALS','health':{k:health.get(k) for k in ('pid','heartbeat_at','status','account_recovered','unknown_count','nav_usdt','equity','global_budget')},'positions':positions,'cash':state.cash,'mapping':mapping,'concurrent_other_trades':concurrent,'funding':funding,'dispatches':dispatches},default=str))
