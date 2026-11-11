import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest
from openpyxl import Workbook

TOOLS = Path(__file__).resolve().parents[2] / 'tools/data'
sys.path.insert(0, str(TOOLS))
from dq02_native import business_keys, field_state, observe, parse_date, scan


def test_numeric_lexemes_and_missing_markers():
    state = field_state('REV','REV [营业收入] - 单位：元')
    for value in ('-1.2300','0','Inf','broken',None,'--','NaN'):
        observe(state,(value,'s',False))
    assert state['numeric_valid']==3
    assert state['numeric_invalid']==1
    assert state['nonfinite']==1
    assert state['negative']==1
    assert state['zero']==1
    assert state['max_decimal_places']==4
    assert state['null_markers']=={'<ABSENT>':1,'--':1,'NaN':1}


def test_versions_and_one_to_many_grains():
    headers=['Stkcd','Accper','Typrep','IfCorrect','DeclareDate','REV']
    assert business_keys('FS_Comins',headers,{})[0]==headers[:-1]
    assert business_keys('其他应收款明细',['Scode','Date','Debtornm','Amtiar'],{})[0]==['Scode','Date','Debtornm']
    assert business_keys('公司曾用名',['Scode','Chgnmdt','Chgnmbef','Chgnmaft'],{})[0]==['Scode','Chgnmdt','Chgnmbef','Chgnmaft']


def test_streaming_duplicates_and_resume(tmp_path):
    book=Workbook()
    sheet=book.active
    sheet.append(['Scode','Date','REV'])
    sheet.append(['000001','2024-03-31',-3])
    sheet.append(['000001','2024-03-31',-3])
    sheet.append(['000001','2024-03-31',4])
    sheet.append([None,'invalid','bad'])
    workbook=tmp_path/'source.xlsx'
    book.save(workbook)
    archive=tmp_path/'archive.7z'
    with zipfile.ZipFile(archive,'w') as z:
        z.write(workbook,'利润表.xlsx')
    stat=archive.stat()
    job=dict(id='unit',source='CNRDS',table='利润表',archive=str(archive),member='利润表.xlsx',identity=dict(bytes=stat.st_size,mtime_ns=stat.st_mtime_ns),dictionaries={'fields.txt':'Date [日期] - YYYY-MM-DD\nREV [收入] - 单位：元'})
    result=scan(job,tmp_path/'private')
    assert result['rows']==4
    stats=result['sheets'][0]
    assert stats['duplicate_keys']==1
    assert stats['duplicate_excess']==2
    assert stats['conflicting_keys']==1
    assert stats['missing_key_rows']==1
    assert stats['fields'][1]['date_invalid']==1
    assert stats['fields'][2]['numeric_invalid']==1
    assert scan(job,tmp_path/'private')==result
    job['identity']['bytes']+=1
    with pytest.raises(ValueError,match='Input identity changed'):
        scan(job,tmp_path/'private')
