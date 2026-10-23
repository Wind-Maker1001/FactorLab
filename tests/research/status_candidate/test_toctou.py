"""Deterministic attacks on actual Adapter.load and CLI export paths."""
import copy,csv,json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import pytest
import qbot.research.universe.status_candidate.adapter as adapter_module
import qbot.research.universe.status_candidate.ingest as ingest
from qbot.research.universe.status_candidate import Adapter,EvidenceError,CertificationRejected,require_claim
from qbot.research.universe.status_candidate.__main__ import run
from qbot.research.universe.status_candidate.ingest import sha256

def writej(p,v):p.write_text(json.dumps(v,ensure_ascii=False,sort_keys=True),encoding='utf-8')
def repin(f):
    for a in f['bundle']['artifacts']:a['sha256']=sha256(Path(a['path']))
    writej(f['path'],f['bundle']);f['sha']=sha256(f['path'])
    return f
def load(f):return Adapter.load(f['path'],f['sha'],allow_synthetic=True)
def csv_fixture(f,layout='TRDSTA_CONFLICT_V1'):
    n=f['native'];p=f['path'].parent/'native.csv'
    row={'symbol':n['security'],'date':n['trading_date'],'raw_zip_sha256':n['native_source_sha256'],'adj_zip_sha256':n['native_state_source_sha256']}
    if layout=='TRDSTA_CONFLICT_V1':
        row.update(native_Trdsta='10.0',native_StateCode='0',native_Filling='0',raw_volume='100',raw_workbook='frozen.xlsx',raw_source_row='7',adj_source_row='8',membership_candidate='True',membership_candidate_source_row='9')
    else:
        row.update(native_raw_record='MISSING',native_adj_state_record='MISSING',raw_coordinate_bounds='7:9',adj_coordinate_bounds='8:10',MEMBERSHIP_CANDIDATE='True',membership_source_row='9')
    with p.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(row));writer.writeheader();writer.writerow(row)
    source=next(a for a in f['bundle']['artifacts'] if a['source_id']=='NATIVE')
    source.update(path=str(p),media_type='CSV')
    f['bundle']['native_sources'][0]['layout']=layout
    f['csv_path']=p
    return repin(f)

def test_bundle_snapshot_rejects_wrong_security_normal_after_transient_replace(make_bundle):
    f=make_bundle();baseline=f['path'].read_bytes();changed=copy.deepcopy(f['bundle'])
    changed['official'][0].update(security='000002.SZ',field='ST_RISK',value='NORMAL')
    parser=adapter_module.read_json;seen=[]
    def schedule(snapshot):
        if Path(snapshot)==f['path']:
            seen.append(snapshot.expected_sha256);writej(f['path'],changed)
            try:return parser(snapshot)
            finally:f['path'].write_bytes(baseline)
        return parser(snapshot)
    with patch.object(adapter_module,'read_json',side_effect=schedule):a=load(f)
    row=a.evaluate('000002.SZ','2024-01-02')
    assert seen==[f['sha']] and row['st_risk_status']=='UNKNOWN' and not row['official_evidence']
    with pytest.raises(CertificationRejected):require_claim(row,'st_risk_status','NORMAL',adapter=a)
    assert a.evaluate('000001.SZ','2024-01-02')['suspension_status']=='SUSPENDED'
    assert f['path'].read_bytes()==baseline

def test_native_json_snapshot_survives_transient_state_replace(make_bundle):
    f=make_bundle(official=False,native_patch={'native_state_code':0,'native_volume':'100'})
    p=f['path'].parent/'native.json';baseline=p.read_bytes();changed=json.loads(baseline)
    changed[0].update(native_state_code=1,native_volume='0')
    parser=adapter_module.read_json;seen=[]
    def schedule(snapshot):
        if Path(snapshot)==p:
            seen.append(snapshot.expected_sha256);writej(p,changed)
            try:return parser(snapshot)
            finally:p.write_bytes(baseline)
        return parser(snapshot)
    with patch.object(adapter_module,'read_json',side_effect=schedule):a=load(f)
    row=a.evaluate('000001.SZ','2024-01-02')
    assert seen and row['suspension_status']=='NOT_SUSPENDED' and row['has_trades']=='TRUE'
    with pytest.raises(CertificationRejected):require_claim(row,'suspension_status','SUSPENDED',adapter=a)
    assert p.read_bytes()==baseline

@pytest.mark.parametrize('layout',['TRDSTA_CONFLICT_V1','P0_GAPS_V1'])
def test_csv_replacement_after_initial_verification_is_rejected(make_bundle,layout):
    f=csv_fixture(make_bundle(official=False),layout);p=f['csv_path'];baseline=p.read_bytes()
    parser=adapter_module.native_csv;seen=[]
    def schedule(artifact,kind):
        if artifact.source_id=='NATIVE':
            p.write_bytes(baseline+b'\nALTERED_RECORD');seen.append(sha256(p))
            try:return parser(artifact,kind)
            finally:p.write_bytes(baseline)
        return parser(artifact,kind)
    with patch.object(adapter_module,'native_csv',side_effect=schedule):
        with pytest.raises(EvidenceError,match='SOURCE_SHA_MISMATCH:NATIVE'):load(f)
    assert seen and p.read_bytes()==baseline

def test_calendar_replacement_before_snapshot_is_rejected(make_bundle):
    f=make_bundle();p=f['path'].parent/'calendar.csv';baseline=p.read_bytes()
    reader=adapter_module.source_bytes
    def schedule(artifact):
        if artifact.source_id=='CAL':
            p.write_bytes(baseline.replace(b'2024-01-04',b'2024-01-05'))
            try:return reader(artifact)
            finally:p.write_bytes(baseline)
        return reader(artifact)
    with patch.object(adapter_module,'source_bytes',side_effect=schedule):
        with pytest.raises(EvidenceError,match='SOURCE_SHA_MISMATCH:CAL'):load(f)
    assert p.read_bytes()==baseline

def test_calendar_snapshot_is_the_only_parsed_calendar(make_bundle):
    f=make_bundle();p=f['path'].parent/'calendar.csv';baseline=p.read_bytes();parser=adapter_module.csv_stream
    def schedule(snapshot):
        p.write_bytes(baseline.replace(b'2024-01-04',b'2024-01-05'))
        try:return parser(snapshot)
        finally:p.write_bytes(baseline)
    with patch.object(adapter_module,'csv_stream',side_effect=schedule):a=load(f)
    assert '2024-01-04' in a.calendar and '2024-01-05' not in a.calendar

def test_persistent_source_change_after_load_is_rejected(make_bundle):
    f=make_bundle();a=load(f);f['source'].write_bytes(f['source'].read_bytes()+b' CHANGED')
    with pytest.raises(EvidenceError,match='SOURCE_SHA_MISMATCH:OFFICIAL'):a.evaluate('000001.SZ','2024-01-02')

def test_different_buffer_cannot_reuse_verified_sha_in_loader(make_bundle):
    f=make_bundle();parser=adapter_module.read_json
    def substitute(snapshot):
        altered=ingest.VerifiedBytes(snapshot.path,snapshot.content+b'\nCHANGED',snapshot.expected_sha256,snapshot.mismatch)
        return parser(altered)
    with patch.object(adapter_module,'read_json',side_effect=substitute):
        with pytest.raises(EvidenceError,match='BUNDLE_SHA_MISMATCH'):load(f)

def test_bundle_sha_mismatch_creates_no_candidate_files_or_receipt(make_bundle,tmp_path):
    f=make_bundle();changed=copy.deepcopy(f['bundle']);changed['scope']='REAL_FROZEN_CANDIDATE'
    writej(f['path'],changed);output=tmp_path/'rejected_export'
    with pytest.raises(EvidenceError,match='BUNDLE_SHA_MISMATCH'):run(f['path'],f['sha'],output)
    assert not output.exists()

def test_transient_native_attack_during_export_leaves_no_receipt(make_bundle,tmp_path):
    f=csv_fixture(make_bundle(official=False));p=f['csv_path'];baseline=p.read_bytes()
    # A small explicit test lineage artifact enables the same real-layout CLI
    # loader without touching any real ZIP or making a financial qualification.
    zp=f['path'].parent/'synthetic_lineage.zip';zp.write_bytes(b'PK_SYNTHETIC_LINEAGE_BYTES_NOT_REAL_MARKET_DATA')
    lineage=copy.deepcopy(f['bundle']['artifacts'][0]);lineage.update(source_id='TEST_ZIP',path=str(zp),sha256=sha256(zp),role='LINEAGE_ZIP',media_type='ZIP',source_version='SYNTHETIC_TOCTOU_REGRESSION_ONLY')
    f['bundle']['artifacts'].append(lineage);f['bundle']['scope']='REAL_FROZEN_CANDIDATE'
    with p.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream);columns=reader.fieldnames;records=list(reader)
    records[0]['raw_zip_sha256']=records[0]['adj_zip_sha256']=lineage['sha256']
    with p.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(records)
    repin(f);baseline=p.read_bytes();parser=adapter_module.native_csv;output=tmp_path/'failed_export'
    def schedule(artifact,layout):
        if artifact.source_id=='NATIVE':
            records[0].update(native_StateCode='1',raw_volume='0')
            with p.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=columns);writer.writeheader();writer.writerows(records)
            try:return parser(artifact,layout)
            finally:p.write_bytes(baseline)
        return parser(artifact,layout)
    with patch.object(adapter_module,'native_csv',side_effect=schedule):
        with pytest.raises(EvidenceError,match='SOURCE_SHA_MISMATCH:NATIVE'):run(f['path'],f['sha'],output)
    assert not output.exists() and p.read_bytes()==baseline

@pytest.mark.parametrize('layout',['CANONICAL_JSON','TRDSTA_CONFLICT_V1','P0_GAPS_V1'])
def test_parsers_have_no_unverified_path_fallback(make_bundle,layout):
    f=make_bundle(official=False)
    if layout!='CANONICAL_JSON':csv_fixture(f,layout)
    json_parser=adapter_module.read_json;csv_parser=ingest.csv_stream;parsed=[]
    def json_guard(snapshot):
        parsed.append(('json',str(Path(snapshot)),snapshot.expected_sha256))
        with patch.object(Path,'open',side_effect=AssertionError('Parser reopened unverified path')):
            return json_parser(snapshot)
    @contextmanager
    def csv_guard(snapshot):
        parsed.append(('csv',str(Path(snapshot)),snapshot.expected_sha256))
        with patch.object(Path,'open',side_effect=AssertionError('Parser reopened unverified path')):
            with csv_parser(snapshot) as stream:yield stream
    with patch.object(adapter_module,'read_json',side_effect=json_guard),patch.object(adapter_module,'csv_stream',side_effect=csv_guard),patch.object(ingest,'csv_stream',side_effect=csv_guard):a=load(f)
    assert len(parsed)==3 and len(a.native)==1
    with pytest.raises(EvidenceError,match='VERIFIED_BYTE_SNAPSHOT_REQUIRED'):json_parser(f['path'])
    with pytest.raises(EvidenceError,match='VERIFIED_BYTE_SNAPSHOT_REQUIRED'):csv_parser(f['path'])

def test_same_verified_bytes_preserve_bom_enum_types_and_claims(make_bundle):
    f=csv_fixture(make_bundle(official=False));f['path'].write_bytes(b'\xef\xbb\xbf'+f['path'].read_bytes());f['sha']=sha256(f['path'])
    a=load(f);row=a.evaluate('000001.SZ','2024-01-02');native=row['native_observation']
    assert type(native['native_trdsta']) is int and native['native_trdsta']==10 and native['native_trdsta_raw']=='10.0'
    assert type(row['membership_candidate']) is bool and row['suspension_status']=='NOT_SUSPENDED' and row['has_trades']=='TRUE'
    assert row['st_risk_status']=='UNKNOWN' and row['execution_eligible']=='UNKNOWN'
    assert require_claim(row,'suspension_status','NOT_SUSPENDED',adapter=a)=='CANDIDATE_FIELD_FACT_ONLY'
