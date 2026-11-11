import importlib.util
import json
from pathlib import Path
import threading
import zipfile
from datetime import date
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

spec=importlib.util.spec_from_file_location('dq02b',Path(__file__).parents[2]/'tools/data/dq02b_columnar.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


def test_exact_candidate_keys():
    t=pa.table({'date':[1,1,2,None],'instrument':['a','a','b','c']})
    result=m.exact_keys([t],['date','instrument'])
    assert result==dict(rows=4,null_key_rows=1,duplicate_keys=1,duplicate_excess=1,unique_nonnull_keys=2)


def test_shared_budget(tmp_path,monkeypatch):
    m.initialize(threading.RLock());monkeypatch.setattr(m,'DISK',10);monkeypatch.setattr(m,'RESERVE',0)
    (tmp_path/'existing').write_bytes(b'123456')
    with pytest.raises(RuntimeError,match='shared 4 GiB'):m.save(tmp_path/'x.json',{'a':1},tmp_path,0)
    assert not (tmp_path/'x.json').exists()


def test_content_and_resume(tmp_path,monkeypatch):
    m.initialize(threading.RLock());monkeypatch.setattr(m,'RESERVE',0);monkeypatch.setattr(m,'BATCH',2)
    src=tmp_path/'source';src.mkdir();state=tmp_path/'state';state.mkdir()
    for n in ('parts','stage','keys'):(state/n).mkdir()
    data=pa.table({'date':[date(2024,1,1)]*4,'instrument':['a','a','b',None],'value':[1.0,float('nan'),float('inf'),None]})
    buf=pa.BufferOutputStream();pq.write_table(data,buf,row_group_size=2)
    with zipfile.ZipFile(src/'stock.zip','w') as z:z.writestr('stock/cn_stock_bar1d/2024-01-01_2024-01-31.parquet',buf.getvalue().to_pybytes())
    job=m.discover(src)[0];result,keys=m.scan(job,state,0)
    assert result['rows']==4 and result['read_row_groups']==2
    assert result['fields']['value']['nan']==1 and result['fields']['value']['inf']==1
    assert result['fields']['value']['nulls']==1
    assert not list((state/'stage').iterdir())
    assert m.exact_keys(keys,result['key_fields'])['duplicate_excess']==1
    assert m.scan(job,state,0)[1] is None
    job['identity']['bytes']+=1
    with pytest.raises(ValueError,match='identity'):m.scan(job,state,0)
