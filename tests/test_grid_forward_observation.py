from copy import deepcopy
from pathlib import Path
import json

import pytest

from factorlab.dataset.identity import json_hash
from factorlab.experiments.grid_forward_evidence import evaluate
from factorlab.experiments.grid_forward_observation import normalize, describe, summarize


ROOT = Path(__file__).resolve().parents[1]


def events(identity='window-a', *, offset_minutes=0):
    from datetime import timedelta
    from factorlab.experiments.grid_forward import utc

    fixture = json.loads((ROOT / 'docs/validation/p7-r12/synthetic-shadow.json').read_text())
    if offset_minutes:
        def shift(value):
            if isinstance(value, dict):
                return {k: shift(v) for k, v in value.items()}
            if isinstance(value, list):
                return [shift(v) for v in value]
            if isinstance(value, str) and value.startswith('2026-'):
                return (utc(value) + timedelta(minutes=offset_minutes)).isoformat()
            return value
        fixture = shift(fixture)
    data = fixture['source']['payload']
    binding = data['binding']
    deployment = {'map': [['account_id', binding['account']]]}
    strategy_hash = json_hash(deployment)
    definition = {'factory': 'P7_M13_GRID_FACTORY'}
    rows = [dict(kind='experiment', started_at=fixture['window'][0], deadline=fixture['window'][1],
        parameters=dict(experiment_id=identity, max_bytes=5242880,
            max_quote_gap_seconds=fixture['max_quote_gap_seconds'],
            strategy_definition=definition, strategy_definition_sha256=json_hash(definition))),
        dict(kind='strategy_binding', deployment_config=deployment, configuration_hash=strategy_hash,
             instrument=binding['instrument'], interval_seconds=binding['interval_seconds'], version=binding['version'])]
    for b in data['bars']:
        from factorlab.experiments.grid_forward import utc
        raw = [int(utc(b['opened_at']).timestamp() * 1000), '100', '102', '99', '101', '1',
               int(utc(b['closed_at']).timestamp() * 1000) - 1]
        receipt = {k: b[k] for k in ('opened_at', 'closed_at', 'received_at', 'known_at', 'completed_bar')}
        receipt.update(interval_seconds=60, sha256=json_hash(raw))
        rows.append(dict(kind='bar_receipt', raw=raw, **receipt))
        rows.append(dict(kind='begin', source=b['id'], at=b['decision_at'], market_at=b['closed_at'],
                         live=True, bar_receipt=receipt))
    rows.append(dict(kind='capture', proposals=[dict(id=p['id'], source=p['bar_id'],
        proposed_at=p['decision_at'], captured_at=p['captured_at'], market_at=p['market_at'],
        deadline=p['deadline'], side=p['side'], limit=p['price'], quantity=p['quantity'],
        configuration_hash=strategy_hash, deployment_config=deployment, version=binding['version'])
        for p in data['proposals']]))
    for b in data['books']:
        raw = dict(symbol=binding['instrument'], time=int(utc(b['market_at']).timestamp() * 1000),
                   bidPrice=b['bid'], askPrice=b['ask'], bidQty=b['bid_quantity'], askQty=b['ask_quantity'])
        rows.append(dict(kind='book', raw=raw, **b))
    rows.extend(dict(kind='end', **e) for e in data['ends'])
    rows.append(dict(kind='window_end', at=fixture['window'][1], reason='RUN_END'))
    return rows


def evaluated(rows):
    evidence = normalize(rows)
    record = json.loads((ROOT / 'docs/validation/p7-r11/protocol.json').read_text())
    return dict(evidence=evidence, description=describe(evidence),
                evaluation=evaluate(record, evidence, evaluated_at='2026-11-16T14:00:00Z'))


def test_r12_reads_normalized_original_proposal():
    window = evaluated(events())
    assert window['evaluation']['engineering_status'] == 'VALID_ENGINEERING_OBSERVATION'
    assert window['evaluation']['metrics'] is None
    assert window['evaluation']['receipt_status'] == 'UNVERIFIED_RECEIPT'
    assert window['description']['counts']['independent_proposals'] == 1
    assert window['description']['counts']['forward_quote_pairs'] == 3
    assert window['description']['counts']['right_censored_proposals'] == 1
    assert window['description']['confirmed_exchange_fills'] == 0


def test_poll_diagnostics_do_not_replace_quotes_or_erase_interruptions():
    rows = events()
    rows.append(dict(kind='poll_round', round=1, total_seconds=4,
                     within_quote_gap=True, quotes=100))
    rows.append(dict(kind='interruption', at=rows[-2]['at'], reason='R13_QUOTE_GAP'))
    window = evaluated(rows)
    assert window['description']['counts']['captured_books'] == sum(r['kind'] == 'book' for r in rows)
    assert window['evaluation']['engineering_status'] == 'INVALID_EVIDENCE'
    assert any(g.get('reason') == 'R13_QUOTE_GAP'
               for g in window['evidence']['source']['payload']['gaps'])


def test_rewrapped_source_proposals_rejected_before_aggregation():
    a, b = evaluated(events()), evaluated(events('window-b'))
    assert a['evaluation']['engineering_status'] == b['evaluation']['engineering_status'] == 'VALID_ENGINEERING_OBSERVATION'
    assert a['description']['counts']['forward_quote_pairs'] == 3
    # Rejection must precede any access to the counts being aggregated.
    b.pop('description')
    with pytest.raises(ValueError, match='duplicate source'):
        summarize([a, b])


def test_independent_windows_do_not_count_quotes_as_proposals():
    a, b = evaluated(events()), evaluated(events('window-b', offset_minutes=10))
    assert a['evaluation']['engineering_status'] == b['evaluation']['engineering_status'] == 'VALID_ENGINEERING_OBSERVATION'
    assert a['evidence']['source']['payload']['proposals'][0]['id'] == b['evidence']['source']['payload']['proposals'][0]['id']
    result = summarize([a, b])
    assert result['counts']['observation_windows'] == 2
    assert result['counts']['independent_proposals'] == 2
    assert result['counts']['forward_quote_pairs'] == 6
    assert result['counts']['captured_books'] == 2 * a['description']['counts']['captured_books']
    assert result['counts']['qualified_books'] == result['counts']['captured_books']
    assert result['counts']['qualified_forward_quote_pairs'] == 6
    with pytest.raises(ValueError, match='duplicate'):
        summarize([a, deepcopy(a)])
    b['evidence']['source']['payload']['strategy_definition_sha256'] = 'different'
    with pytest.raises(ValueError, match='incompatible'):
        summarize([a, b])


@pytest.mark.parametrize('field,value', [
    ('id', 'renamed'), ('source', 'renamed-bar'), ('limit', '10000'),
    ('quantity', '2'), ('proposed_at', '2026-11-15T12:29:02Z'),
    ('captured_at', '2026-11-15T12:29:03Z')])
def test_single_proposal_edit_does_not_create_independent_market_sample(field, value):
    rows = events('window-b')
    proposal = next(e for e in rows if e['kind'] == 'capture')['proposals'][0]
    proposal[field] = value
    if field == 'source':
        next(e for e in rows if e['kind'] == 'begin')['source'] = value
    with pytest.raises(ValueError, match='duplicate source'):
        summarize([evaluated(events()), evaluated(rows)])


def test_quote_reuse_rejected_even_with_independent_proposal_and_bars():
    a = evaluated(events())
    b = evaluated(events('window-b', offset_minutes=10))
    b['evidence']['source']['payload']['books'] = deepcopy(a['evidence']['source']['payload']['books'])
    with pytest.raises(ValueError, match='duplicate source quote'):
        summarize([a, b])


def test_bar_reuse_rejected_without_shared_quotes():
    a = evaluated(events())
    b = evaluated(events('window-b'))
    b['evidence']['source']['payload']['books'] = []
    b['evidence']['source']['payload']['proposals'][0]['id'] = 'renamed'
    with pytest.raises(ValueError, match='duplicate source Bar slice'):
        summarize([a, b])


def test_same_window_id_rejected_even_for_distinct_sources():
    with pytest.raises(ValueError, match='duplicate observation window'):
        summarize([evaluated(events()), evaluated(events(offset_minutes=10))])


def test_identical_evidence_rejected():
    a = evaluated(events())
    with pytest.raises(ValueError, match='duplicate observation window'):
        summarize([a, deepcopy(a)])


def test_frozen_r13_sources_and_rejections_unchanged():
    from factorlab.experiments.grid_forward_observation import read_events

    root = ROOT / 'docs/validation/p7-r13'
    windows = []
    for index, name in enumerate(('window-a', 'window-b'), 1):
        frozen = json.loads((root / f'normalized/window-{index}.json').read_text())
        evidence = normalize(read_events(root / name / 'events.csv'))
        assert evidence == frozen['evidence']
        current = evaluated(read_events(root / name / 'events.csv'))
        assert current['description'] == frozen['description']
        assert current['evaluation']['issues'] == frozen['evaluation']['issues']
        assert current['evaluation']['sample_status'] == 'INSUFFICIENT_SAMPLE'
        assert current['description']['confirmed_exchange_fills'] == 0
        windows.append(current)
    summary = summarize(windows)
    assert summary['counts'] == json.loads((root / 'normalized/summary.json').read_text())['counts']
    assert summary['counts']['qualified_forward_quote_pairs'] == 0


@pytest.mark.parametrize('kind', ['resume', 'interruption', 'rejected_book'])
def test_interruptions_never_disappear(kind):
    rows = events()
    rows.append(dict(kind=kind, at='2026-11-15T12:30:00Z', reason='TEST_GAP'))
    window = evaluated(rows)
    assert window['evaluation']['sample_status'] == 'INSUFFICIENT_SAMPLE'
    assert window['evaluation']['engineering_metrics'] is None
    assert 'DECLARED_OBSERVATION_GAP' in {i['code'] for i in window['evaluation']['issues']}
    assert summarize([window])['counts']['qualified_books'] == 0


def test_raw_receipt_cannot_be_changed_independently():
    rows = events()
    next(e for e in rows if e['kind'] == 'bar_receipt')['raw'][4] = '999'
    with pytest.raises(ValueError, match='raw Bar digest'):
        normalize(rows)
    rows = events()
    next(e for e in rows if e['kind'] == 'book')['raw']['askPrice'] = '999'
    with pytest.raises(ValueError, match='raw book'):
        normalize(rows)


def test_missing_bar_knowledge_is_not_inferred_from_close():
    rows = events()
    next(e for e in rows if e['kind'] == 'begin')['bar_receipt'] = None
    window = evaluated(rows)
    assert 'MISSING_BAR_RECEIPT' in {i['code'] for i in window['evaluation']['issues']}


def test_time_and_internal_quote_gate_remain_r12_owned():
    rows = events()
    begin = next(e for e in rows if e['kind'] == 'begin')
    begin['at'] = '2026-11-15T12:27:00Z'
    window = evaluated(rows)
    assert window['evaluation']['engineering_status'] == 'INVALID_TIMELINE'
    rows = events()
    rows[0]['parameters']['max_quote_gap_seconds'] = 1
    window = evaluated(rows)
    assert 'QUOTE_GAP' in {i['code'] for i in window['evaluation']['issues']}


def test_missing_top_quantity_stays_missing():
    rows = events()
    book = next(e for e in rows if e['kind'] == 'book')
    book['bid_quantity'] = None
    book['raw'].pop('bidQty')
    evidence = normalize(rows)
    assert evidence['source']['payload']['books'][0]['bid_quantity'] is None
    assert evidence['source']['payload']['gaps'][0]['kind'] == 'MISSING_TOP_QUANTITY'


@pytest.mark.parametrize('change', ['early_end', 'missing_end', 'unanswered_get'])
def test_incomplete_window_is_not_repaired(change):
    rows = events()
    if change == 'early_end':
        rows[-1]['at'] = rows[0]['started_at']
    elif change == 'missing_end':
        rows.pop()
    else:
        rows.append(dict(kind='request', method='GET', endpoint='/fapi/v1/time'))
    window = evaluated(rows)
    assert window['evaluation']['sample_status'] == 'INSUFFICIENT_SAMPLE'
    assert window['evaluation']['engineering_metrics'] is None


def test_observed_crossing_is_not_exchange_fill():
    rows = events()
    proposal = next(e for e in rows if e['kind'] == 'capture')['proposals'][0]
    proposal['limit'] = '10000'
    window = evaluated(rows)
    observation = window['description']['proposals'][0]
    assert observation['midpoint_touch']
    assert observation['potential_opposite_book_crossing']
    assert observation['first_touch_delay_seconds'] > 0
    assert not observation['right_censored']
    assert window['description']['confirmed_exchange_fills'] == 0
    assert window['evaluation']['metrics'] is None


@pytest.mark.parametrize('missing_quantity', [False, True])
def test_offline_cli_roundtrip_and_missing_quantity_rejection(tmp_path, missing_quantity):
    import csv
    from factorlab.experiments.grid_forward_observation import main

    rows = events()
    if missing_quantity:
        b = next(e for e in rows if e['kind'] == 'book')
        b['bid_quantity'] = None
        b['raw'].pop('bidQty')
    path = tmp_path / 'events.csv'
    with path.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['event'])
        writer.writerows([[json.dumps(e)] for e in rows])
    output = tmp_path / 'normalized'
    main(['--events', str(path), '--protocol', str(ROOT / 'docs/validation/p7-r11/protocol.json'),
          '--evaluated-at', '2026-11-15T12:31:00Z', '--output', str(output)])
    window = json.loads((output / 'window-1.json').read_text())
    assert window['evaluation']['engineering_status'] == (
        'INVALID_EVIDENCE' if missing_quantity else 'VALID_ENGINEERING_OBSERVATION')
    assert window['evaluation']['metrics'] is None
    with pytest.raises(FileExistsError):
        main(['--events', str(path), '--protocol', str(ROOT / 'docs/validation/p7-r11/protocol.json'),
              '--evaluated-at', '2026-11-15T12:31:00Z', '--output', str(output)])


def test_strategy_and_definition_identity_checked():
    rows = events()
    rows[0]['parameters']['strategy_definition']['factory'] = 'TUNED'
    with pytest.raises(ValueError, match='strategy definition'):
        normalize(rows)
    rows = events()
    next(e for e in rows if e['kind'] == 'capture')['proposals'][0]['configuration_hash'] = 'TUNED'
    with pytest.raises(ValueError, match='proposal strategy'):
        normalize(rows)
