from pathlib import Path
import json
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'tools/data'))
from dq02_finalize import exact_table,table_name


def test_exact_cross_partition_conflicts_and_unknown_keys(tmp_path):
    receipts=[]
    for i,records in enumerate([
        [('a','1'),('b','2'),(None,'unknown')],
        [('a','1'),('b','3'),(None,'unknown')],
    ]):
        path=tmp_path/f'{i}.sqlite'
        db=sqlite3.connect(path)
        db.execute('create table rows(key text,payload text,partition text)')
        db.execute('create table enums(field text,value text,count integer)')
        db.execute('insert into enums values(?,?,?)',('status',str(i),2))
        db.executemany('insert into rows values(?,?,?)',[(json.dumps([key]) if key else None,json.dumps({'instrument':key,'value':value}),str(i)) for key,value in records])
        db.commit();db.close()
        receipts.append(dict(id=str(i),status='FULL_CONTENT_SCANNED',sqlite_path=str(path),enum_stats={'status':{}},job={'member':str(i)},key_coverage=dict(fields=['instrument'],null_key_rows=1)))
    stats=exact_table(tmp_path,'test',receipts)
    assert stats['checked_rows']==6
    assert stats['key_covered_rows']==4
    assert stats['missing_key_rows']==2
    assert stats['duplicate_keys']==2
    assert stats['duplicate_excess_rows']==2
    assert stats['conflicting_keys']==1
    assert stats['overlapping_partition_keys']==2
    assert stats['enum_changes'][0]['added_values']==1
    assert stats['enum_changes'][0]['removed_values']==1


def test_native_column_order_is_not_payload_conflict(tmp_path):
    receipts=[]
    for i,(headers,payload) in enumerate([(['Scode','Date','value'],['a','2024-01-01','1']),(['value','Date','Scode'],['1','2024-01-01','a'])]):
        path=tmp_path/f'{i}.sqlite'
        db=sqlite3.connect(path)
        db.execute('create table records(sheet text,key text,payload text,subject text)')
        db.execute('insert into records values(?,?,?,?)',('S',json.dumps(['a','2024-01-01']),json.dumps(payload),'a'))
        db.commit();db.close()
        receipts.append(dict(id=str(i),status='FULL_CONTENT_SCANNED',database=str(path),sheets=[dict(sheet='S',keys=['Scode','Date'],headers=headers)]))
    stats=exact_table(tmp_path,'native',receipts)
    assert stats['duplicate_keys']==1
    assert stats['conflicting_keys']==0


def test_table_family_denominators():
    assert table_name('CSMAR','FS_Combas12')=='FS_Combas'
    assert table_name('CNRDS','个股日回报率-2012_2','CNSP_个股日回报率_ALL.zip')=='个股日回报率'
    assert table_name('CNRDS','利润表2','利润表.7z')=='利润表'
