"""Original self-contained synthetic fixture; no vendor data."""
from pathlib import Path
import json
import pytest
from qbot.research.universe.status_candidate.ingest import sha256
ROOT=Path(__file__).resolve().parent
def write_json(path,obj):
    path.write_text(json.dumps(obj,sort_keys=True,ensure_ascii=False),encoding="utf-8")

@pytest.fixture
def make_bundle(tmp_path):
    count=[0]
    def factory(*,native_patch=None,official_patch=None,official=True,symbol='000001.SZ',relations=None):
        count[0]+=1;p=tmp_path/str(count[0]);p.mkdir()
        cal=p/'calendar.csv';cal.write_text('date\n2024-01-02\n2024-01-03\n2024-01-04\n',encoding='utf-8')
        source=p/'official.json';write_json(source,{'SYNTHETIC_TEST_ONLY':'No actual issuer or exchange assertion'})
        native={'security':symbol,'trading_date':'2024-01-02','native_trdsta':10,'native_trdsta_raw':'10.0','native_label':'U','native_state_code':None,'native_filling':0,'native_volume':None,'native_source_sha256':'a'*64,'native_source_coordinate':'SYNTHETIC raw row1','native_state_source_sha256':'b'*64,'native_state_source_coordinate':'SYNTHETIC state row1','native_missing':False,'native_missing_reason':'SYNTHETIC_TEST_OBSERVATION','record_source_id':'NATIVE','record_coordinate':'JSON_ROW:1','dictionary_binding':'UNVERIFIED','membership_candidate':True,'membership_source_coordinate':'SYNTHETIC membership row1','source_ids':['NATIVE']}
        native.update(native_patch or {})
        nf=p/'native.json';write_json(nf,[native])
        ev={'evidence_id':'SYNTHETIC_E1','security':symbol,'field':'SUSPENSION','value':'SUSPENDED','effective_from':'2024-01-02','effective_to':'2024-01-02','declared_dates':['2024-01-02'],'implemented':True,'announcement_id':'SYNTHETIC_NO_REAL_ANNOUNCEMENT','source_ids':['OFFICIAL'],'original_document_location':'SYNTHETIC_JSON_FIXTURE','event_date':'2024-01-02','announced_at':None,'source_published_at':None,'first_available_at':None,'retrieved_at':'2026-10-21T00:00:00+00:00','historical_known_at':None,'evidence_scope':'SYNTHETIC_TEST_ONLY_NOT_REAL_AUTHORITY'}
        ev.update(official_patch or {})
        artifacts=[]
        for sid,path,role,media in [('CAL',cal,'CALENDAR','CSV'),('NATIVE',nf,'NATIVE_CANDIDATE','JSON'),('OFFICIAL',source,'OFFICIAL_DOCUMENT','JSON')]:
            artifacts.append({'source_id':sid,'path':str(path),'sha256':sha256(path),'role':role,'media_type':media,'url':None,'source_version':'SYNTHETIC_TEST_ONLY','locator':'SYNTHETIC_NOT_REAL_EVIDENCE','retrieved_at':'2026-10-21T00:00:00+00:00'})
        bundle={'schema':'fl-status-candidate/v1','scope':'SYNTHETIC_TEST','artifacts':artifacts,'native_sources':[{'source_id':'NATIVE','layout':'CANONICAL_JSON'}],'official':[ev] if official else [],'relationships':relations or [],'calendar_source_id':'CAL','forbidden_output_roots':[str(ROOT/'protected_test_root')]}
        bp=p/'bundle.json';write_json(bp,bundle)
        return {'path':bp,'sha':sha256(bp),'native':native,'official':ev,'source':source,'bundle':bundle}
    return factory
