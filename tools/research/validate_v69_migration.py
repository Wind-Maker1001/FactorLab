"""Reproducible same-input oracle evidence, not a full historical replay.
Run: python tools/research/validate_v69_migration.py
"""
import importlib.util
import csv
import json
from pathlib import Path
import sys
import numpy as np
import pandas as pd
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
spec=importlib.util.spec_from_file_location('v69_oracle_tests',ROOT/'tests/research/test_v69_equivalence.py')
t=importlib.util.module_from_spec(spec);spec.loader.exec_module(t)
out=ROOT/'docs/migration/v69-f02';out.mkdir(parents=True,exist_ok=True)
patch=pytest.MonkeyPatch();fixture=t.oracle.__wrapped__(patch);Oracle=next(fixture)
rows=0;maximum=0.
try:
    t.test_all_source_hashes()
    for cash in (100000.,300000.): t.test_order_differential(Oracle,cash)
    for case in ('outside','invalid_held','zero_exit','deadband','cap_sell','cap_buy'):
        t.test_intent_edges(Oracle,case)
    t.test_fixture_is_reference_output_only()
    close,volume=t.history()
    with (out/'weight-differences.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['session','omit_sndk','holding_case','symbol','reference','native','delta'])
        for omit in (False,True):
            old,new=Oracle(close,volume,omit_sndk=omit),t.NativeV69(close,volume,omit_sndk=omit)
            for day in close.index:
                if day.weekday()!=0: continue
                prices=close.loc[day];valid=np.isfinite(prices)&prices.gt(0)
                for case,held in enumerate([pd.Series(0.,index=t.TICKERS),pd.Series(10.,index=t.TICKERS),pd.Series([0,1,0,2,0,3,0,4],index=t.TICKERS,dtype=float)]):
                    a,b=old.targets(day,prices,valid,held),new.targets(day,prices,valid,held)
                    pd.testing.assert_series_equal(a,b)
                    for symbol in t.TICKERS:
                        delta=float(b[symbol]-a[symbol]);maximum=max(maximum,abs(delta));rows+=1
                        writer.writerow([str(day.date()),omit,case,symbol,a[symbol],b[symbol],delta])
    from types import SimpleNamespace
    with (out/'order-differences.csv').open('w',newline='') as f:
        writer=csv.writer(f);writer.writerow(['session','cash','order_index','symbol','side','reference_quantity','native_quantity','delta'])
        for cash in (100000.,300000.):
            old,new=Oracle(close,volume),t.V69Planner(close,volume)
            for i,day in enumerate(close.index[1:],1):
                holdings={symbol:float((i+j)%7) for j,symbol in enumerate(t.TICKERS)}
                if i<=126:holdings['SNDK']=0.
                if i==151:holdings['BA']=0.
                for obj in (old,new):
                    obj.trade_position=SimpleNamespace(get_stock_amount_dict=lambda:holdings,get_cash=lambda:cash)
                    obj.trade_calendar=SimpleNamespace(get_step_time=lambda:(day,day))
                a,b=old.generate_trade_decision(),new.generate_trade_decision()
                assert a.order_list==b.order_list
                for n,(x,y) in enumerate(zip(a.order_list,b.order_list)):
                    writer.writerow([str(day.date()),cash,n,x.stock_id,x.direction,x.amount,y.amount,y.amount-x.amount])
    result=dict(SOURCE_AVAILABLE='PASS: all 27 manifest entries byte/hash verified',
                SIGNAL_EQUIVALENCE='PASS: deterministic same-input synthetic oracle; not historical market replay',
                ORDER_INTENT_EQUIVALENCE='PASS: same-input oracle and explicit edge tests',
                FULL_REFERENCE_REPLAY='PENDING: original fixture/market and Qlib Provider absent',
                ALPACA_INTEGRATION='PENDING_PUBLIC_DEPENDENCY: see integration-snapshots.json and INTEGRATION.md; this command validates only strategy equivalence',
                weight_diff_rows=rows,max_absolute_weight_difference=maximum,
                reference_artifact_counts=dict(sessions=977,targets=204,fills=371),
                historical_inputs_replayed=False)
    (out/'validation-status.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
finally:
    try: next(fixture)
    except StopIteration: pass
    patch.undo()
