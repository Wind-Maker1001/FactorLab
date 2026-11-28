from types import SimpleNamespace

import pytest

from factorlab.integrations.alpaca.corporate_actions import PreparationIncomplete, read_announcements


def test_saturated_window_is_partitioned_without_overlapping_dates_or_lost_ids():
    calls = []
    shared = {'id': 'boundary', 'ca_type': 'split', 'effective_date': '2026-11-24'}

    def request(method, path, *, params):
        calls.append((params['since'], params['until']))
        assert method == 'GET'
        assert path == '/v2/corporate_actions/announcements'
        if len(calls) == 1:
            return [shared] + [{'id': str(i)} for i in range(1450)]
        indices = range(725) if params['since'] == '2026-11-20' else range(725, 1450)
        return [shared] + [{'id': str(i)} for i in indices]

    result = read_announcements(SimpleNamespace(request=request), since='2026-11-20', until='2026-11-30')
    assert calls == [('2026-11-20', '2026-11-30'), ('2026-11-20', '2026-11-25'),
                     ('2026-11-26', '2026-11-30')]
    assert result == [shared] + [{'id': str(i)} for i in range(1450)]
    assert 'ex_date' not in result[0]


def test_missing_date_blocks_only_an_affected_strategy_asset():
    from factorlab.integrations.alpaca.cloud_execution import affected_assets
    other = dict(id='official-other', initiating_symbol='IPFX', target_symbol='IPFXU')
    assert affected_assets([other], '2026-11-30') == set()
    relevant = dict(other, id='official-owned', target_symbol='AAPL')
    with pytest.raises(PreparationIncomplete, match='CORPORATE_ACTION_EX_DATE_REQUIRED:official-owned'):
        affected_assets([relevant], '2026-11-30')


def test_saturated_single_day_remains_blocked():
    http = SimpleNamespace(request=lambda *args, **kwargs: [{'id': str(i)} for i in range(1000)])
    with pytest.raises(PreparationIncomplete, match='SATURATED_CORPORATE_ACTION_DAY:2026-11-20'):
        read_announcements(http, since='2026-11-20', until='2026-11-20')


@pytest.mark.parametrize('child', [[], [{'id': '0', 'cash': 'changed'}]])
def test_parent_evidence_cannot_disappear_or_change_during_partition(child):
    def request(*args, params):
        if params['since'] != params['until']:
            return [{'id': str(i)} for i in range(1000)]
        return child

    with pytest.raises(PreparationIncomplete, match='CORPORATE_ANNOUNCEMENT_(DISAPPEARED|CHANGED):'):
        read_announcements(SimpleNamespace(request=request), since='2026-11-20', until='2026-11-21')


def test_conflicting_official_identity_is_not_silently_deduplicated():
    def request(*args, params):
        if params['since'] != params['until']:
            return [{'id': str(i)} for i in range(1000)]
        return [{'id': 'same', 'cash': params['since']}]

    with pytest.raises(PreparationIncomplete, match='CORPORATE_ANNOUNCEMENT_CHANGED:same'):
        read_announcements(SimpleNamespace(request=request), since='2026-11-20', until='2026-11-21')
