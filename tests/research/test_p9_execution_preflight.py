"""Synthetic diagnostic boundaries; no financial result fixtures."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from factorlab.data.market_evidence_sidecar import CandidateFact, MarketEvidence, SAFETY

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('preflight', ROOT / 'tools/research/p9_execution_preflight.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


@pytest.fixture
def market():
    days = pd.bdate_range('2023-04-24', periods=8)
    bars = pd.DataFrame(dict(date=days, symbol='002408.SZ', raw_open=10., raw_high=11.,
        raw_low=9., raw_close=10., volume=100, pre_close=10., upper_limit=11., lower_limit=9.,
        adjust_factor=1., source_sha256='a' * 64, source_member='synthetic/price'))
    status = pd.DataFrame(dict(date=days, symbol='002408.SZ', suspended=0, st_status=0,
        is_risk_warning=0, price_limit_status=0, exdr=0,
        source_sha256='b' * 64, source_member='synthetic/state'))
    members = bars[p.KEYS].copy()
    return bars, status, members, days


def diagnostic(market, evidence=None):
    bars, status, members, days = market
    panel, duplicates = p.make_panel(bars, status, members, days, ['002408.SZ'])
    return p.diagnose(panel, days, dict.fromkeys(days, 0), duplicates, evidence)


def test_duplicate_keys_are_not_resolved_to_a_price(market):
    bars, status, members, days = market
    bars = pd.concat([bars, bars.iloc[[2]].assign(raw_close=500)], ignore_index=True)
    local, risks, *_ = diagnostic((bars, status, members, days))
    duplicate = risks.loc[risks.research_impact.eq('DUPLICATE_KEY')]
    assert len(duplicate) == 1
    assert duplicate.raw_value.iloc[0] == '2'
    assert pd.isna(local.loc[local.date.eq(days[2]), 'raw_close']).all()


@pytest.mark.parametrize('field,impact', [('raw_open', 'MISSING_OPEN'), ('raw_close', 'MISSING_CLOSE'),
                                         ('raw_high', 'NONFINITE_VALUE'), ('volume', 'NONFINITE_VALUE')])
def test_missing_fields(market, field, impact):
    market[0].loc[2, field] = np.nan
    _, risk, *_ = diagnostic(market)
    assert ((risk.source_field == field) & (risk.research_impact == impact)).any()


def test_missing_bar_and_missing_state_are_separate(market):
    bars, status, members, days = market
    _, risks, *_ = diagnostic((bars.drop(index=2), status.drop(index=3), members, days))
    assert ((risks.date == str(days[2].date())) & (risks.research_impact == 'MISSING_BAR')).any()
    assert ((risks.date == str(days[3].date())) & (risks.research_impact == 'SOURCE_STATE_MISSING')).any()


def test_suspension_filled_prices_are_not_real_observations(market):
    market[0].loc[2:3, 'volume'] = 0
    market[1].loc[2:3, 'suspended'] = 1
    local, risk, gaps, *_ = diagnostic(market)
    assert not local.loc[local.date.isin(market[3][2:4]), 'observed_close'].any()
    assert len(gaps) == 1 and gaps.sessions.iloc[0] == 2
    assert gaps.last_real_close_date.iloc[0] == str(market[3][1].date())
    assert gaps.next_real_close_date.iloc[0] == str(market[3][4].date())
    assert (risk.issue_type == 'KNOWN_NONTRADABLE').sum() == 2


@pytest.mark.parametrize('field,value', [('raw_low', 12), ('raw_high', 8), ('raw_open', -1)])
def test_invalid_ohlc(market, field, value):
    market[0].loc[2, field] = value
    assert diagnostic(market)[1].research_impact.eq('INVALID_OHLC').any()


def test_unknown_enum_is_not_source_missing(market):
    market[1].loc[2, 'st_status'] = 2
    _, risk, _, enums, *_ = diagnostic(market)
    row = risk.loc[(risk.issue_type == 'UNSUPPORTED_STATUS_ENUM') & (risk.source_field == 'st_status')]
    assert row.raw_value.tolist() == ['2']
    assert not ((risk.date == str(market[3][2].date())) & (risk.research_impact == 'SOURCE_STATE_MISSING')).any()
    assert 'ADAPTER_DISCARDS_SEMANTICS_UNVERIFIED' in enums.classification.tolist()


def test_missing_sidecar_does_not_approve_policy(market):
    market[0].loc[2, 'raw_close'] = np.nan
    _, risk, gaps, _, coverage, _ = diagnostic(market)
    assert coverage.empty
    assert gaps.sidecar_covered_sessions.sum() == 0
    assert not gaps.approved_for_frozen_p9_01.any()
    assert risk.sidecar_coverage.eq('NONE').all()


def test_sidecar_exact_scope_is_index_not_permission(market):
    market[0].loc[2, 'raw_close'] = np.nan
    fact = CandidateFact('002408.SZ', '2023-04-26', 'valuation_mark', None, '10',
        'MODEL_VALUATION_ASSUMPTION', 'a' * 64, '2026-11-11', 'UNKNOWN',
        'OBSERVATION_IDENTITY_BOUND', 'RESEARCH_CANDIDATE_ONLY', 'No execution permit')
    evidence = MarketEvidence((fact,), 'c' * 64)
    _, risk, gaps, _, coverage, _ = diagnostic(market, evidence)
    assert gaps.sidecar_covered_sessions.sum() == 1
    assert not coverage.trading_permission.any()
    assert not coverage.policy_approved_for_frozen_p9_01.any()
    assert set(risk.loc[risk.sidecar_coverage.ne('NONE'), 'date']) == {'2023-04-26'}


def test_valuation_runs_and_previous_real_close(market):
    market[0].loc[[1, 2, 5], 'raw_close'] = np.nan
    _, _, gaps, _, _, intervals = diagnostic(market)
    assert gaps.sessions.tolist() == [2, 1]
    assert gaps.last_real_close_date.tolist() == [str(market[3][0].date()), str(market[3][4].date())]
    assert gaps.next_real_close_date.tolist() == [str(market[3][3].date()), str(market[3][6].date())]
    assert sum(r['sessions'] for r in intervals) == 8


def test_member_exit_is_not_delisting(market):
    bars, status, members, days = market
    local, risk, *_ = diagnostic((bars, status, members.iloc[:3], days))
    assert len(local) == len(days)
    assert risk.research_impact.eq('HISTORICAL_MEMBER_EXIT_NOT_DELISTING_PROOF').sum() == 1


def test_future_anomalies_cannot_mutate_signal_inputs(market):
    original = deepcopy(market)
    market[0].loc[4:, 'raw_close'] = np.nan
    market[1].loc[4:, 'st_status'] = 2
    diagnostic(market)
    pd.testing.assert_frame_equal(market[0].iloc[:4], original[0].iloc[:4])
    pd.testing.assert_frame_equal(market[2], original[2])
    config = json.loads((ROOT / 'configs/research/p9-d02/preflight.json').read_bytes())
    assert config['selection_filter_permitted'] is False
    source = (ROOT / 'tools/research/p9_execution_preflight.py').read_text(encoding='utf-8')
    assert 'make_targets' not in source and 'signal_candidates(' not in source
    assert 'run_target_weights' not in source and 'make_runner' not in source


def test_public_export_removes_commercial_prices_and_rows(market):
    market[0].loc[2, 'raw_close'] = np.nan
    local, risk, gaps, enums, coverage, intervals = diagnostic(market)
    summary = p.public_summary(local, risk, gaps, enums, coverage, {}, [], intervals, market[3], ['002408.SZ'])
    with TemporaryDirectory() as directory:
        private = Path(directory) / 'private'
        private.mkdir()
        p.dump(private / 'summary.json', summary)
        p.dump(private / 'data-coverage.json', summary['securities'])
        p.dump(private / 'valuation-intervals.json', intervals)
        for name, frame in [('execution-risk', risk), ('valuation-gaps', gaps), ('status-enums', enums), ('sidecar-coverage', coverage)]:
            if frame.empty and name == 'sidecar-coverage':
                frame = pd.DataFrame(columns=['date', 'symbol', 'field'])
            frame.to_csv(private / (name + '.csv'), index=False)
        output = Path(directory) / 'public'
        p.publish(private, output)
        published = pd.read_csv(output / 'execution-risk.csv')
        assert published.raw_value.eq('REDACTED_COMMERCIAL_OBSERVATION').all()
        assert not {'last_real_close', 'next_real_close'} & set(pd.read_csv(output / 'valuation-gaps.csv').columns)
        assert 'private_artifact_sha256' in json.loads((output / 'summary.json').read_bytes())
        assert all('raw_open' not in row for row in json.loads((output / 'data-coverage.json').read_bytes()))


def test_private_rows_cannot_be_written_inside_repository():
    with pytest.raises(ValueError, match='PRIVATE_OUTPUT_MUST_BE_OUTSIDE_REPOSITORY'):
        p.require_private_output(ROOT / 'docs/validation/p9-d02-preflight/raw')


def test_frozen_input_pin_drift():
    config = json.loads((ROOT / 'configs/research/p9-d02/preflight.json').read_bytes())
    config['inputs']['manifest']['lf_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='INPUT_HASH_DRIFT'):
        p.frozen_inputs(config)


def test_input_hash_drift():
    with pytest.raises(ValueError, match='INPUT_HASH_DRIFT'):
        p.checked(b'changed', p.digest(b'original'))


@pytest.mark.parametrize('symbols', [['002408'], ['002408.SH'], ['600001.SZ'], ['../../bad'],
                                    ['002408.SZ', '002408.SZ'], ['002408.XSHE']])
def test_illegal_security_identity(symbols):
    with pytest.raises(ValueError, match='ILLEGAL_SECURITY_IDENTITY'):
        p.validate_symbols(symbols)


def test_frozen_contract_and_gate(market):
    config = json.loads((ROOT / 'configs/research/p9-d02/preflight.json').read_bytes())
    loaded = p.frozen_inputs(config)
    assert len(loaded['manifest']['symbols']) == 128
    assert loaded['freeze']['folds'][0]['test'][0] == '2023-03-13'
    local, risk, gaps, enums, coverage, intervals = diagnostic(market)
    summary = p.public_summary(local, risk, gaps, enums, coverage, {}, [], intervals, market[3], ['002408.SZ'])
    assert summary['safety'] == list(SAFETY)
    assert summary['formal_admission'] is False and summary['live_money'] is False
    assert summary['selection_filter_permitted'] is False
    assert summary['consumption'] == ['BLOCKING_WARNING', 'VALUATION_EVIDENCE_INDEX']
    assert not {'raw_value', 'raw_close', 'last_real_close'} & summary['securities'][0].keys()
    assert 'source_member' not in summary['securities'][0]


def test_no_ledger_means_not_assessed(market):
    local, risk, gaps, *_ = diagnostic(market)
    out = p.holding_exposure(SimpleNamespace(study=None), {}, local, risk, gaps)
    assert out['status'] == 'HOLDING_EXPOSURE_NOT_ASSESSED'


def test_nonfinite_not_zero_and_price_jump_candidate(market):
    market[0].loc[2, 'raw_close'] = np.inf
    market[0].loc[4, 'raw_close'] = 30
    market[0].loc[4, 'raw_high'] = 31
    _, risk, *_ = diagnostic(market)
    assert ((risk.source_field == 'raw_close') & (risk.research_impact == 'NONFINITE_VALUE')).any()
    assert risk.research_impact.eq('ABS_RAW_CLOSE_JUMP_GT_20_PERCENT').any()


def test_ledger_hash_drift_fails_before_exposure(market, tmp_path):
    folder = tmp_path / 'B_factor_score-fold-0'
    folder.mkdir()
    (folder / 'positions.parquet').write_bytes(b'changed')
    local, risk, gaps, *_ = diagnostic(market)
    loaded = {'p9_result': {'private_artifact_sha256': {'B_factor_score-fold-0/positions.parquet': p.digest(b'original')}}}
    with pytest.raises(ValueError, match='INPUT_HASH_DRIFT'):
        p.holding_exposure(SimpleNamespace(study=tmp_path), loaded, local, risk, gaps)
