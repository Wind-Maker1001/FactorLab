"""Research input rejects unsafe execution windows without changing observations."""
import json

import pandas as pd
import pytest

from factorlab.data.evidence import sha256
from factorlab.data.hub import DataHub
from factorlab.data.hub.contracts import UnsupportedQueryError
from factorlab.data.research_input import quality, prepare, ResearchInputAdapter
from factorlab.errors import SchemaError


def bars():
    return pd.DataFrame(dict(date=pd.date_range('2015-01-05', periods=4), symbol=['600028.SH']*4,
        raw_open=[10.]*4, raw_high=[11.]*4, raw_low=[9.]*4, raw_close=[10.]*4,
        volume=[100.]*4, trade_status=[1]*4, is_st=[0]*4, adj_close=[1000.]*4))


@pytest.mark.parametrize('field', ['date','symbol','raw_open','raw_high','raw_low','raw_close','volume','trade_status','is_st'])
def test_every_execution_field_required(field):
    with pytest.raises(SchemaError):
        quality(bars().drop(columns=field), ['600028.SH'], '2015-01-01', '2015-12-31')


def test_raw_fields_and_ohlc_required():
    b = bars()
    with pytest.raises(SchemaError):
        quality(b.drop(columns='raw_open'), ['600028.SH'], '2015-01-01', '2015-12-31')
    b.loc[1, 'raw_high'] = 8
    b.loc[2, 'raw_close'] = float('inf')
    result = quality(b, ['600028.SH'], '2015-01-01', '2015-12-31')
    assert result['ready_rows'] == 2
    assert result['rejected_reasons'] == {'INVALID_OHLC_OR_VOLUME': 1, 'MISSING_OR_NONFINITE_RAW_FIELDS': 1}


def test_suspension_missing_and_boundaries():
    b = bars().drop(index=2)
    b.loc[1, ['trade_status','volume']] = [0,0]
    b.loc[1, ['raw_open','raw_high','raw_low','raw_close']] = float('nan')
    result = quality(b, ['600028.SH', '601857.SH'], '2015-01-01', '2015-12-31')
    assert result['securities'][1]['status'] == 'BLOCKED'
    assert result['securities'][0]['listing_boundary'].endswith('UNKNOWN')
    assert result['rejected_rows'] == 1
    assert result['securities'][0]['intervals'][0]['sessions'] == 1
    with pytest.raises(SchemaError):
        quality(pd.concat([b,b]), ['600028.SH'], '2015-01-01', '2015-12-31')


def test_pinned_replay_and_hub(tmp_path):
    source, raw, output = (tmp_path / n for n in ('source','raw','out'))
    source.mkdir(); raw.mkdir()
    b = bars()
    b.loc[1, 'trade_status'] = 0
    b.to_parquet(source / 'bars.parquet', index=False)
    payload = dict(schema='P5_R03_CSMAR_PRIVATE_V1', bars_sha256=sha256((source/'bars.parquet').read_bytes()), references=[])
    (source/'manifest.json').write_text(json.dumps(payload), encoding='utf-8')
    config = tmp_path/'config.json'
    config.write_text(json.dumps(dict(source_manifest_sha256=sha256((source/'manifest.json').read_bytes()),
        symbols=['600028.SH'], start='2015-01-01', end='2015-12-31')), encoding='utf-8')
    prepare(config, source, raw, output)
    pinned = sha256((output/'manifest.json').read_bytes())
    adapter = ResearchInputAdapter(output, source, pinned)
    hub = DataHub({'research': adapter})
    query = dict(symbol='600028.SH', start='2015-01-07', end='2015-01-08')
    result = hub.read('research', 'daily_bars', **query)
    assert result.data.raw_open.tolist() == [10.,10.]
    assert 'adj_close' not in result.data
    assert 'FORMAL_ADMISSION_BLOCKED' in result.metadata.evidence_status
    with pytest.raises(UnsupportedQueryError):
        hub.read('research', 'daily_bars', **{**query, 'start':'2015-01-05'})
    with pytest.raises(UnsupportedQueryError):
        hub.read('research', 'daily_bars', knowledge_as_of='2015-01-09T00:00:00Z', **query)
    original_manifest = (output/'manifest.json').read_bytes()
    promoted = json.loads(original_manifest)
    promoted['historical_pit'] = True
    (output/'manifest.json').write_text(json.dumps(promoted), encoding='utf-8')
    with pytest.raises(SchemaError, match='research-only'):
        ResearchInputAdapter(output, source, sha256((output/'manifest.json').read_bytes()))
    (output/'manifest.json').write_bytes(original_manifest)
    b.loc[3, 'raw_open'] = 9.
    b.to_parquet(source/'bars.parquet', index=False)
    with pytest.raises(SchemaError, match='bars identity'):
        ResearchInputAdapter(output, source, pinned)
