"""Synthetic admission checks, not financial or exchange evidence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from factorlab.dataset.identity import json_hash
from factorlab.experiments.grid_forward import engineering_protocol, freeze
from factorlab.experiments.grid_forward_evidence import (
    SCHEMA, FINANCIAL_REQUIREMENTS, evaluate, financial_preflight, publish, read_m13,
)
from factorlab.experiments.tracker import ExperimentTracker

FROZEN = '2026-11-15T00:00:00Z'
NOW = '2026-11-15T00:05:00Z'
ROOT = Path(__file__).resolve().parents[1]


def sample():
    data = dict(
        binding=dict(account='SYNTHETIC_ACCOUNT', instrument='NVDAUSDT',
                     strategy='synthetic-grid-v1', version='synthetic-code-v1',
                     interval_seconds=60, session_started_at=FROZEN, prior_bar_ids=[]),
        bars=[dict(id='bar-1', opened_at=FROZEN, closed_at='2026-11-15T00:01:00Z',
                   received_at='2026-11-15T00:01:01Z', known_at='2026-11-15T00:01:01Z',
                   decision_at='2026-11-15T00:01:02Z', completed_bar=True, live=True)],
        proposals=[dict(id='p-1', bar_id='bar-1', market_at='2026-11-15T00:01:00Z',
                        decision_at='2026-11-15T00:01:02Z', captured_at='2026-11-15T00:01:03Z',
                        deadline='2026-11-15T00:02:00Z', side='BUY', price='100', quantity='1',
                        account='SYNTHETIC_ACCOUNT', strategy='synthetic-grid-v1',
                        version='synthetic-code-v1', replay=False)],
        books=[dict(market_at=f'2026-11-15T00:01:{s:02d}Z',
                    received_at=f'2026-11-15T00:01:{s+1:02d}Z',
                    bid='101', ask='102', bid_quantity='3', ask_quantity='4')
               for s in (10, 30, 50)], fills=[], gaps=[], ends=[])
    return dict(schema=SCHEMA, kind='FORWARD_SHADOW',
                window=[FROZEN, '2026-11-15T00:02:00Z'], max_quote_gap_seconds=25,
                source=dict(format='NORMALIZED_V1', name='SYNTHETIC_ENGINEERING_ONLY',
                            sha256=json_hash(data), payload=data))


@pytest.fixture
def record(tmp_path):
    return freeze(tmp_path / 'r11', engineering_protocol('synthetic-v1', FROZEN))


def seal(evidence):
    evidence['source']['sha256'] = json_hash(evidence['source']['payload'])
    return evidence


def codes(result):
    return {i['code'] for i in result['issues']}


def test_legal_shadow_censoring_and_identity(record):
    evidence = sample()
    result = evaluate(record, evidence, evaluated_at=NOW)
    assert result == evaluate(record, evidence, evaluated_at=NOW)
    assert result['engineering_status'] == 'VALID_ENGINEERING_OBSERVATION'
    metrics = result['engineering_metrics']
    assert metrics['valid_bars'] == metrics['valid_forward_proposals'] == 1
    assert metrics['observable_touches'] == metrics['book_candidates'] == 0
    assert metrics['right_censored_ratio'] == 1
    assert metrics['bar_coverage']['ratio'] == .5
    assert metrics['coverage']['ratio'] == 1
    assert metrics['duration_seconds'] == [57]
    assert metrics['sampled_absolute_midpoint_deviation_bps'] == {'min': '150.000', 'max': '150.000'}
    assert result['receipt_status'] == 'UNVERIFIED_RECEIPT'
    assert result['sample_status'] == 'NOT_ASSESSED'
    assert result['financial_admission'] == 'NOT_FINANCIALLY_ADMITTED'
    assert result['metrics'] is None
    assert result['activation']['active'] is False


@pytest.mark.parametrize('target,field,value,code', [
    ('bars', 'opened_at', '2026-08-12T00:00:00Z', 'INVALID_TIMELINE'),
    ('bars', 'completed_bar', False, 'UNCLOSED_BAR'),
    ('bars', 'known_at', '2026-11-15T00:00:59Z', 'INVALID_TIMELINE'),
    ('bars', 'decision_at', '2026-11-15T00:00:59Z', 'INVALID_TIMELINE'),
    ('bars', 'live', False, 'HISTORICAL_REPLAY'),
    ('bars', 'closed_at', '2026-11-15T00:01:30Z', 'BAR_INTERVAL_MISMATCH'),
    ('books', 'market_at', '2026-11-16T00:00:00Z', 'INVALID_TIMELINE'),
    ('books', 'market_at', '2026-11-15T00:02:00Z', 'INVALID_TIMELINE'),
    ('books', 'received_at', '2026-11-15T00:00:59Z', 'INVALID_TIMELINE'),
    ('proposals', 'replay', True, 'HISTORICAL_REPLAY'),
    ('proposals', 'strategy', 'changed', 'PROPOSAL_BINDING_CONFLICT'),
    ('proposals', 'bar_id', 'absent', 'MISSING_DECISION_BAR'),
])
def test_invalid_declarations_never_produce_qualified_metrics(record, target, field, value, code):
    evidence = sample()
    evidence['source']['payload'][target][0][field] = value
    result = evaluate(record, seal(evidence), evaluated_at=NOW)
    assert code in codes(result)
    assert result['engineering_metrics'] is None
    assert result['metrics'] is None
    assert result['sample_status'] == 'INSUFFICIENT_SAMPLE'


@pytest.mark.parametrize('target,code', [
    ('bars', 'DUPLICATE_OR_CONFLICTING_BAR'),
    ('proposals', 'DUPLICATE_OR_CONFLICTING_PROPOSAL'),
    ('books', 'QUOTE_DUPLICATE_OR_ORDER'),
])
def test_duplicates_and_conflicts_are_not_repaired(record, target, code):
    evidence = sample()
    evidence['source']['payload'][target].append(deepcopy(evidence['source']['payload'][target][0]))
    assert code in codes(evaluate(record, seal(evidence), evaluated_at=NOW))


def test_gaps_missing_quotes_and_restart_replay(record):
    for mutate, code in [
        (lambda d: d['books'].clear(), 'MISSING_FORWARD_QUOTE'),
        (lambda d: d['books'].pop(1), 'QUOTE_GAP'),
        (lambda d: d['books'].pop(0), 'QUOTE_EDGE_GAP'),
        (lambda d: d['gaps'].append({'reason': 'PROCESS_RESTART'}), 'DECLARED_OBSERVATION_GAP'),
        (lambda d: d['binding']['prior_bar_ids'].append('bar-1'), 'HISTORICAL_REPLAY'),
        (lambda d: d['books'].reverse(), 'QUOTE_DUPLICATE_OR_ORDER'),
    ]:
        evidence = sample()
        mutate(evidence['source']['payload'])
        result = evaluate(record, seal(evidence), evaluated_at=NOW)
        assert code in codes(result)
        assert result['engineering_metrics'] is None


@pytest.mark.parametrize('received_at,max_gap,valid', [
    ('2026-11-15T00:02:01Z', 25, False),
    ('2026-11-15T00:02:01Z', '39.999999', False),
    ('2026-11-15T00:02:01Z', 40, True),
    ('2026-11-15T00:02:01Z', '40.000001', True),
    ('2026-11-15T00:01:31Z', 25, True),
])
def test_proposal_quote_gaps_after_receipt_filtering(record, received_at, max_gap, valid):
    evidence = sample()
    evidence['max_quote_gap_seconds'] = max_gap
    evidence['source']['payload']['books'][1]['received_at'] = received_at
    result = evaluate(record, seal(evidence), evaluated_at=NOW)
    if valid:
        assert result['issues'] == []
        assert result['engineering_status'] == 'VALID_ENGINEERING_OBSERVATION'
        assert result['engineering_metrics']['coverage']['ratio'] == 1
        assert result['diagnostics']['quote_gaps'] == 0
    else:
        assert result['issues'] == [{'code': 'PROPOSAL_QUOTE_GAP', 'identity': 'p-1'}]
        assert result['engineering_status'] == 'INVALID_EVIDENCE'
        assert result['engineering_metrics'] is None
        assert result['sample_status'] == 'INSUFFICIENT_SAMPLE'
        assert result['diagnostics']['quote_gaps'] == 1
    assert result['metrics'] is None
    assert result['financial_admission'] == 'NOT_FINANCIALLY_ADMITTED'
    assert result['activation']['active'] is False
    assert result['safety'] == 'NO LIVE MONEY'


def test_interruption_end_cannot_hide_a_gap(record):
    evidence = sample()
    evidence['source']['payload']['ends'] = [dict(
        at='2026-11-15T00:01:55Z', ids=['p-1'], reason='DATA_INTERRUPTION')]
    result = evaluate(record, seal(evidence), evaluated_at=NOW)
    assert 'TERMINAL_OBSERVATION_INTERRUPTION' in codes(result)
    assert result['engineering_metrics'] is None


def test_proposal_order_and_noncontiguous_bars(record):
    evidence = sample()
    data = evidence['source']['payload']
    p = deepcopy(data['proposals'][0])
    p.update(id='p-2', captured_at='2026-11-15T00:01:04Z')
    data['proposals'].insert(0, p)
    assert 'PROPOSAL_ORDER' in codes(evaluate(record, seal(evidence), evaluated_at=NOW))
    evidence = sample()
    b = deepcopy(evidence['source']['payload']['bars'][0])
    b.update(id='bar-2', opened_at='2026-11-15T00:01:30Z',
             closed_at='2026-11-15T00:02:30Z', received_at='2026-11-15T00:02:31Z',
             known_at='2026-11-15T00:02:31Z', decision_at='2026-11-15T00:02:32Z')
    evidence['source']['payload']['bars'].append(b)
    assert 'BAR_GAP_DUPLICATE_OR_ORDER' in codes(evaluate(record, seal(evidence), evaluated_at=NOW))


def test_historical_and_summary_integrity(record):
    evidence = sample()
    evidence['kind'] = 'HISTORICAL_REFERENCE'
    assert 'HISTORICAL_REFERENCE_ONLY' in codes(evaluate(record, evidence, evaluated_at=NOW))
    evidence = sample()
    evidence['source']['payload']['books'][0]['bid'] = '99'
    assert 'SOURCE_DIGEST_MISMATCH' in codes(evaluate(record, evidence, evaluated_at=NOW))
    evidence = sample()
    evidence['summary'] = dict(proposals=2, books=3, confirmed_exchange_fills=0)
    assert 'SOURCE_SUMMARY_MISMATCH' in codes(evaluate(record, evidence, evaluated_at=NOW))


def test_shadow_touch_not_fill_and_no_paper_promotion(record):
    evidence = sample()
    for book in evidence['source']['payload']['books']:
        book.update(bid='98', ask='99')
    result = evaluate(record, seal(evidence), evaluated_at=NOW)
    assert result['engineering_metrics']['observable_touches'] == 1
    assert result['engineering_metrics']['book_candidates'] == 1
    assert result['engineering_metrics']['right_censored_ratio'] == 0
    assert result['metrics'] is None
    evidence['source']['payload']['fills'] = [{'source': 'SHADOW_TOUCH'}]
    result = evaluate(record, seal(evidence), evaluated_at=NOW)
    assert 'SHADOW_OR_HISTORY_CANNOT_CONTAIN_FILLS' in codes(result)
    assert result['engineering_metrics'] is None
    evidence['kind'] = 'FORWARD_PAPER'
    with pytest.raises(ValueError, match='category'):
        evaluate(record, evidence, evaluated_at=NOW)


def demo():
    evidence = sample()
    evidence['kind'] = 'DEMO_CONFIRMED'
    fill = dict(orderId='exchange-order-1', tradeId='exchange-trade-1',
                source='EXCHANGE_CONFIRMED', confirmed_at='2026-11-15T00:01:21Z',
                market_at='2026-11-15T00:01:20Z', quantity='0.5', price='99', fee='0.01',
                fee_asset='USDT', account='SYNTHETIC_ACCOUNT', proposal_id='p-1', integrity='COMPLETE',
                venue='BINANCE_USDM_DEMO', endpoint='/fapi/v1/userTrades',
                received_at='2026-11-15T00:01:21Z')
    fill['receipt'] = json_hash(fill)
    evidence['source']['payload']['fills'] = [fill]
    return seal(evidence)


def test_full_demo_structure_still_requires_external_verification(record):
    result = evaluate(record, demo(), evaluated_at=NOW)
    assert result['engineering_status'] == 'VALID_ENGINEERING_OBSERVATION'
    assert result['receipt_status'] == 'UNVERIFIED_RECEIPT'
    assert result['metrics'] is None
    assert result['financial_status'] == 'FINANCIAL_PROTOCOL_PENDING_APPROVAL'


@pytest.mark.parametrize('field', ['orderId', 'tradeId', 'source', 'confirmed_at', 'quantity',
                                   'price', 'fee', 'fee_asset', 'account', 'integrity', 'receipt'])
def test_demo_missing_confirmation(record, field):
    evidence = demo()
    del evidence['source']['payload']['fills'][0][field]
    result = evaluate(record, seal(evidence), evaluated_at=NOW)
    assert 'MISSING_EXCHANGE_CONFIRMATION' in codes(result)
    assert result['metrics'] is None


def test_demo_without_fills_and_forged_receipt(record):
    evidence = sample()
    evidence['kind'] = 'DEMO_CONFIRMED'
    assert 'MISSING_EXCHANGE_CONFIRMATION' in codes(evaluate(record, evidence, evaluated_at=NOW))
    evidence = demo()
    evidence['source']['payload']['fills'][0]['quantity'] = '2'
    result = evaluate(record, seal(evidence), evaluated_at=NOW)
    assert {'FILL_RECEIPT_DIGEST_MISMATCH', 'FILLED_QUANTITY_EXCEEDS_PROPOSAL'} <= codes(result)


@pytest.mark.parametrize('mutation', ['hash', 'identity', 'canonical', 'definition'])
def test_r11_tampering_even_with_recomputed_hash(record, mutation):
    if mutation == 'hash':
        record['sha256'] = '0' * 64
    else:
        if mutation == 'identity':
            record['id'] = 'r11-altered'
        elif mutation == 'canonical':
            record['payload']['parameters']['financial_status'] = 'APPROVED'
            record['id'] = record['experiment_id'] = 'r11-' + json_hash(record['payload']['parameters'])
        else:
            record['payload']['costs'] = {'fee': 0}
        record['sha256'] = json_hash({k: v for k, v in record.items() if k != 'sha256'})
    with pytest.raises(ValueError, match='R11'):
        evaluate(record, sample(), evaluated_at=NOW)


def test_financial_handoff_checks_shared_terms_without_activation():
    assert financial_preflight({})['structural_status'] == 'INCOMPLETE'
    candidate = dict.fromkeys(FINANCIAL_REQUIREMENTS, 'SYNTHETIC_DECLARATION')
    candidate.update(capital='1000', start=FROZEN, end=NOW, common_termination_at=NOW)
    shared = ('capital', 'notional_budget', 'costs', 'start', 'end', 'common_termination_at')
    candidate['accounts'] = {name: dict({k: candidate[k] for k in shared},
                                       artifact_identities={'hash': 'synthetic'})
                             for name in ('grid_original', 'fixed_hold', 'lagged_exposure', 'cash')}
    result = financial_preflight(candidate)
    assert result['structural_status'] == 'DECLARED_ONLY'
    assert result['active'] is False and result['metrics'] is None
    candidate['accounts']['fixed_hold']['capital'] = '2000'
    assert 'SHARED_TERMS_MISMATCH:fixed_hold' in financial_preflight(candidate)['problems']
    candidate['approved_immutable_version'] = 'r11-existing'
    assert 'R11_CANNOT_BE_ACTIVATED' in financial_preflight(candidate)['problems']


def test_empty_sample_and_naive_time(record):
    evidence = sample()
    for key in ('bars', 'proposals', 'books'):
        evidence['source']['payload'][key] = []
    result = evaluate(record, seal(evidence), evaluated_at=NOW)
    assert result['sample_status'] == 'INSUFFICIENT_SAMPLE'
    assert result['engineering_metrics']['right_censored_ratio'] is None
    with pytest.raises(ValueError, match='UTC'):
        evaluate(record, evidence, evaluated_at='2026-11-15T00:05:00')


def test_real_m13_pre_freeze_proposal_is_rejected():
    record = json.loads((ROOT / 'docs/validation/p7-r11/protocol.json').read_text())
    evidence = read_m13(ROOT / 'docs/validation/p7-m13/real/events.csv',
                        start='2026-11-15T12:26:00Z', end='2026-11-15T12:30:00Z',
                        max_quote_gap_seconds=30)
    evidence['summary'] = json.loads((ROOT / 'docs/validation/p7-m13/real/summary.json').read_text())
    result = evaluate(record, evidence, evaluated_at='2026-11-15T12:31:00Z')
    assert result['diagnostics']['input_proposals'] == 1
    assert {'INVALID_TIMELINE', 'M13_BAR_KNOWLEDGE_NOT_PUBLISHED'} <= codes(result)
    assert 'SOURCE_SUMMARY_MISMATCH' not in codes(result)
    assert result['engineering_metrics'] is None
    assert result['metrics'] is None
    evidence['kind'] = 'DEMO_CONFIRMED'
    result = evaluate(record, evidence, evaluated_at='2026-11-15T12:31:00Z')
    assert {'SHADOW_CANNOT_BECOME_EXECUTION', 'MISSING_EXCHANGE_CONFIRMATION'} <= codes(result)


def test_tracker_publishes_r12_and_binds_result_without_changing_r11(record, tmp_path):
    evidence = sample()
    original = deepcopy(record)
    result = evaluate(record, evidence, evaluated_at=NOW)
    kwargs = dict(code={'commit': 'a' * 40, 'tree': 'b' * 40}, environment={'synthetic': True},
                  command=['synthetic-engineering-test'], test_status='SUCCEEDED')
    published = publish(tmp_path / 'r12', record, evidence, result, **kwargs)
    tracker = ExperimentTracker(tmp_path / 'r12')
    assert tracker.get_run(published['run']['id']) == published['run']
    assert published['experiment']['payload']['parameters']['protocol_hash'] == record['sha256']
    assert published['run']['payload']['run']['portfolio_metrics'] == {}
    assert record == original
    with pytest.raises(FileExistsError):
        publish(tmp_path / 'r12', record, evidence, result, **kwargs)
    result['metrics'] = {'profit': 123}
    with pytest.raises(ValueError, match='does not match'):
        publish(tmp_path / 'different', record, evidence, result, **kwargs)
