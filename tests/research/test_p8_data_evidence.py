"""Bounded inspection cannot promote snapshots into financial qualification."""
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from factorlab.data.admission import assert_formal_market_admission
from factorlab.errors import ProviderError, SchemaError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('p8_data_evidence', ROOT / 'tools/research/p8_data_evidence.py')
p8 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p8)
SEM = dict(unit='CNY/share', basis='RAW', meaning='raw_close')


def bars():
    return pd.DataFrame(dict(date=pd.to_datetime(['2018-01-02', '2018-01-03']),
                             symbol=['601857.SH'] * 2, raw_close=[8.0, 8.1]))


def test_pinned_identity_and_drift(tmp_path):
    path = tmp_path / 'input'
    path.write_bytes(b'original')
    expected = p8.sha256(b'original')
    assert p8.pinned(path, expected) == b'original'
    path.write_bytes(b'revised')
    with pytest.raises(SchemaError, match='identity drift'):
        p8.pinned(path, expected)


def test_repository_text_eol_is_explicit_not_binary_fallback(tmp_path):
    path = tmp_path / 'tracked.json'
    path.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')
    expected = p8.sha256(b'{\n  "a": 1\n}\n')
    assert p8.pinned_repository_text(path, expected) == b'{\n  "a": 1\n}\n'
    with pytest.raises(SchemaError):
        p8.pinned(path, expected)
    path.write_bytes(b'{\n  "a": 2\n}\n')
    with pytest.raises(SchemaError):
        p8.pinned_repository_text(path, expected)


def test_matched_mismatch_and_missing_dates():
    left, right = bars(), bars()
    right.loc[0, 'raw_close'] = 8.2
    out = p8.compare(left, right, 'raw_close', SEM, SEM)
    assert out['MATCHED'] == out['MISMATCH'] == 1
    out = p8.compare(left, right.iloc[:1], 'raw_close', SEM, SEM)
    assert out['SOURCE_MISSING'] == 1
    assert sum(out.values()) == 2


def test_missing_field_and_null_are_not_zero():
    left, right = bars(), bars()
    right['raw_close'] = [0.0, float('nan')]
    out = p8.compare(left, right, 'raw_close', SEM, SEM)
    assert out['MISMATCH'] == 1 and out['INSUFFICIENT_EVIDENCE'] == 1
    out = p8.compare(left, right.drop(columns='raw_close'), 'raw_close', SEM, SEM)
    assert out['INSUFFICIENT_EVIDENCE'] == 2


@pytest.mark.parametrize('changed', [dict(unit='USD/share'), dict(unit=None), dict(basis='HFQ'), dict(meaning='adjusted_close')])
def test_unit_and_price_semantics_conflicts(changed):
    out = p8.compare(bars(), bars(), 'raw_close', SEM, {**SEM, **changed})
    assert out['SEMANTICALLY_INCOMPARABLE'] == 2
    assert out['MATCHED'] == 0


def test_status_null_association_does_not_certify_semantics():
    raw = bars().assign(raw_open=[float('nan'), 8.1], raw_high=[float('nan'), 8.2],
                        raw_low=[float('nan'), 8.0], trade_status=[0, 1])
    raw.loc[0, 'raw_close'] = float('nan')
    state = bars()[['date', 'symbol']].assign(suspended=[1, 0])
    out = p8.status_association(raw, state)
    assert out['code_1_and_missing_raw_ohlc'] == out['code_1_and_csmar_nontrading_proxy'] == 1
    assert 'CERTIFICATION_UNKNOWN' in out['meaning']
    assert p8.status_association(raw, state.drop(columns='suspended'))['status'] == 'UNKNOWN'


def test_duplicate_keys_rejected():
    with pytest.raises(SchemaError, match='duplicate'):
        p8.compare(pd.concat([bars(), bars()]), bars(), 'raw_close', SEM, SEM)


def receipt_and_evidence():
    evidence = json.loads((ROOT / 'configs/research/p6-e01-601857-2018.json').read_bytes())
    receipt = json.loads((ROOT / 'docs/validation/p6-e01/receipt.json').read_bytes())
    return receipt, evidence


def test_saved_return_definitions_unknown_tax_and_null():
    receipt, evidence = receipt_and_evidence()
    out = p8.return_status(receipt, evidence)
    assert out['coverage_status'] == out['known_at_evidence'] == out['revision_evidence'] == 'UNKNOWN'
    rsi = out['strategies']['rsi']
    assert float(rsi['bounded_gross_dividend_entitlement']) == 0
    assert rsi['actual_received_cash'] is None and rsi['tax_amount'] is None
    assert rsi['annual_pretax_total_return'] is None and rsi['annual_aftertax_total_return'] is None
    assert out['strategies']['buy_hold']['bounded_gross_dividend_entitlement'] == '1839.34200'
    assert out['strategies']['dual_ma']['record_close_holdings'][1]['eligible_shares'] == 13100
    assert rsi['same_execution_path_explicit_costs'] > 0


def test_wrong_return_source_rejected():
    receipt, evidence = receipt_and_evidence()
    receipt['comparison_manifest_sha256'] = '0' * 64
    with pytest.raises(SchemaError, match='source combination'):
        p8.return_status(receipt, evidence)


@pytest.mark.parametrize('revision', [None, 2])
def test_event_duplicates_or_revisions_not_collapsed(revision):
    receipt, evidence = receipt_and_evidence()
    evidence['events'].append({**evidence['events'][0], **({'revision': revision} if revision is not None else {})})
    versions = p8.event_versions(evidence['events'])
    assert versions['duplicate_versions'] + versions['multiple_versions'] == 1
    assert versions['revision_chain'] == 'UNKNOWN'
    with pytest.raises(SchemaError, match='event versions'):
        p8.return_status(receipt, evidence)


def test_saved_entitlement_drift_rejected():
    receipt, evidence = receipt_and_evidence()
    receipt['strategies']['buy_hold']['events'][0]['eligible_shares'] += 1
    with pytest.raises(SchemaError, match='entitlement mismatch'):
        p8.return_status(receipt, evidence)


@pytest.mark.parametrize('raw,bound,events,expected', [
    (False, False, False, 0), (False, True, False, 1),
    (True, True, False, 2), (True, True, True, 3), (True, False, True, 0)])
def test_capabilities_are_conjunctions_not_formal_admission(raw, bound, events, expected):
    out = p8.capability(raw, bound, events)
    assert out['level'] == expected
    assert out['formal_admission'] is out['historical_pit'] is out['hfq_verified'] is False
    with pytest.raises(ProviderError, match='qualification'):
        assert_formal_market_admission(out, membership=None, calendar=None, bars=None, benchmark=None,
                                       research_start='2018-01-01', research_end='2018-12-31')


def archive_fixture(tmp_path, *, wrong_table=False, absent_field=False):
    path = tmp_path / 'private-token-secret.zip'
    table = 'stock/cn_stock_real_bar1d'
    name = table + '/2018-01-01_2018-01-31.parquet'
    frame = bars().rename(columns={'symbol': 'instrument', 'raw_close': 'close'})
    if absent_field:
        frame = frame.drop(columns='close')
    buf = BytesIO()
    frame.to_parquet(buf, index=False)
    data = buf.getvalue()
    receipt = json.dumps(dict(table='wrong' if wrong_table else 'cn_stock_real_bar1d', category='stock',
        sha256=p8.sha256(data), exported_at_utc='2026-11-07T12:19:52+00:00')).encode()
    schema = b'{"fields": []}'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr(name, data)
        z.writestr(name + '.receipt.json', receipt)
        z.writestr(table + '/schema.json', schema)
    cfg = dict(columns=['date', 'instrument', 'close'], schema_sha256=p8.sha256(schema), members=[
        dict(member=name, sha256=p8.sha256(data), receipt_sha256=p8.sha256(receipt))])
    return path, table, cfg


def test_archive_identity_unknown_known_at_and_privacy(tmp_path):
    path, table, cfg = archive_fixture(tmp_path)
    frame, binding = p8.archive_table(path, table, cfg, ['601857.SH'], '2018-01-01', '2018-12-31')
    assert len(frame) == 2 and binding['historical_known_at'] is None
    text = json.dumps(binding)
    assert 'private-token-secret' not in text and str(tmp_path) not in text
    assert binding['revision_chain'] is None
    cfg['members'][0]['sha256'] = '0' * 64
    with pytest.raises(SchemaError, match='receipt combination'):
        p8.archive_table(path, table, cfg, ['601857.SH'], '2018-01-01', '2018-12-31')


def test_wrong_archive_source_rejected(tmp_path):
    path, table, cfg = archive_fixture(tmp_path, wrong_table=True)
    with pytest.raises(SchemaError, match='receipt combination'):
        p8.archive_table(path, table, cfg, ['601857.SH'], '2018-01-01', '2018-12-31')


def test_archive_missing_field_reported(tmp_path):
    path, table, cfg = archive_fixture(tmp_path, absent_field=True)
    frame, binding = p8.archive_table(path, table, cfg, ['601857.SH'], '2018-01-01', '2018-12-31')
    assert binding['missing_fields'] == ['close'] and 'close' not in frame


def test_receipt_identity_drift(tmp_path):
    path, table, cfg = archive_fixture(tmp_path)
    cfg['members'][0]['receipt_sha256'] = '0' * 64
    with pytest.raises(SchemaError, match='receipt identity drift'):
        p8.archive_table(path, table, cfg, ['601857.SH'], '2018-01-01', '2018-12-31')


def test_outputs_do_not_overwrite_or_include_private_paths(tmp_path):
    out = tmp_path / 'new'
    report = dict(status='P8_03_PARTIAL', returns=None, inputs={'source': '0' * 64},
                  coverage=[], comparisons=[], objects={})
    p8.write_outputs(report, out)
    assert json.loads((out / 'return-status.json').read_text()) is None
    with pytest.raises(FileExistsError):
        p8.write_outputs(report, out)
    assert str(tmp_path) not in (out / 'summary.json').read_text()
