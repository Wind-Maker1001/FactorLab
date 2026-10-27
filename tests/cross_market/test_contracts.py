import copy
from dataclasses import replace
import numpy as np
import pandas as pd
import pytest

from factorlab.cross_market.contracts import ContractError, ResearchSpec, instant, session_date
from factorlab.cross_market.calendar import us_calendar, cn_calendar
from factorlab.cross_market.identity import IdentityRegistry, DynamicUniverse
from factorlab.cross_market.adapters import (AccountPolicy, ActionLedger, DatedFees, FeePeriod, ChinaTradingRules, USTradingRules, UnknownActions, legacy_csi500_backtest)
from factorlab.cross_market.synthetic import build_us
from factorlab.cross_market.factors import compute_instrument_batch, FACTOR_METADATA
from factorlab.cross_market.data import TaskCache, ParquetDataProvider, CSVDataProvider
from factorlab.cross_market.engine import ExecutionEngine, target_weights


@pytest.fixture(scope='module')
def fixture():
    return build_us(count=72)


def test_symbol_change_retains_identity(fixture):
    _, a = fixture
    reg = a.universe.identities
    assert reg.resolve('S3', 'US', 'XNYS', '2024-06-07', '2024-06-07T22:00Z') == reg.resolve('RENAMED', 'US', 'XNYS', '2024-06-10', '2024-06-10T22:00Z')
    with pytest.raises(ContractError):
        reg.resolve('S3', 'US', 'XNYS', '2024-06-10', '2024-06-10T22:00Z')


def test_symbol_reuse_and_cross_market(fixture):
    _, a = fixture
    f = a.universe.identities.aliases.loc[lambda f: f.instrument_id.eq('fixture:US:3')].copy()
    new = f.iloc[0].copy()
    new['instrument_id'], new['source_identity'] = 'other-listing', 'S3:new-listing'
    new['valid_from'], new['valid_to'], new['listing_date'] = pd.Timestamp('2024-06-10'), pd.NaT, pd.Timestamp('2024-06-10')
    cn = new.copy()
    cn['instrument_id'], cn['market'], cn['exchange'], cn['currency'] = 'CN:source:listing', 'CN', 'XSHG', 'CNY'
    r = IdentityRegistry(pd.concat([f, pd.DataFrame([new, cn])], ignore_index=True))
    assert r.resolve('S3', 'US', 'XNYS', '2024-06-10', '2024-06-11T00:00Z') == 'other-listing'
    assert r.resolve('S3', 'CN', 'XSHG', '2024-06-10', '2024-06-11T00:00Z') == 'CN:source:listing'


def test_identity_overlap_rejected(fixture):
    f = fixture[1].universe.identities.aliases.iloc[:1]
    with pytest.raises(ContractError, match='overlapping'):
        IdentityRegistry(pd.concat([f, f]))


def test_future_identity_unavailable(fixture):
    reg = fixture[1].universe.identities
    with pytest.raises(ContractError):
        reg.resolve('S71', 'US', 'XNYS', '2024-04-01', '2010-01-01T00:00Z')


def test_dynamic_listing_delisting_and_no_current_list(fixture):
    a = fixture[1]
    n = lambda day: set(a.universe.members(day, day + 'T22:00Z').instrument_id)
    assert 'fixture:US:71' not in n('2024-03-28')
    assert 'fixture:US:71' in n('2024-04-01')
    assert 'fixture:US:4' in n('2024-07-31')
    assert 'fixture:US:4' not in n('2024-08-01')


@pytest.mark.parametrize('kind,exchange', [('ETF', 'XNAS'), ('ADR', 'XNYS'), ('REIT', 'XNYS'), ('preferred_stock', 'XNAS'), ('common_stock', 'ARCX')])
def test_us_security_type_scope(fixture, kind, exchange):
    f = fixture[1].universe.identities.aliases.iloc[:1].copy()
    f['security_type'], f['exchange'] = kind, exchange
    assert DynamicUniverse(IdentityRegistry(f), 'US', 'ALL_US_COMMON').members('2024-04-01', '2024-04-01T20:00Z').empty


@pytest.mark.parametrize('day,expected', [('2024-03-08', 14), ('2024-03-11', 13), ('2024-11-01', 13), ('2024-11-04', 14)])
def test_dst(fixture, day, expected):
    assert fixture[1].calendar.at(day, 'open').hour == expected


@pytest.mark.parametrize('day', ['2024-01-01', '2024-03-29', '2024-06-19', '2024-07-04', '2024-12-25'])
def test_us_holidays(fixture, day):
    assert not fixture[1].calendar.is_session(day)


def test_early_close_and_emergency_closure():
    c = us_calendar('2012-10-01', '2024-12-31')
    assert not c.is_session('2012-10-29') and not c.is_session('2012-10-30')
    assert c.at('2024-11-29', 'close').hour == 18


def test_cn_dates_unaffected_by_us_dst():
    c = cn_calendar(pd.DatetimeIndex(['2024-03-08', '2024-03-11']), 'fixture')
    assert c.at('2024-03-08', 'open').hour == c.at('2024-03-11', 'open').hour == 1


@pytest.mark.parametrize('value', ['2024-01-01T00:00Z', '2024-01-01T12:00', pd.NaT])
def test_session_labels_are_not_instants(value):
    with pytest.raises(ContractError):
        session_date(value)


def test_instants_require_timezone():
    with pytest.raises(ContractError):
        instant('2024-01-01')


@pytest.mark.parametrize('day,n', [('2016-06-01', 3), ('2017-09-01', 3), ('2017-09-05', 2), ('2024-05-24', 2), ('2024-05-28', 1)])
def test_settlement_effective_history(day, n):
    assert USTradingRules().settlement_sessions(day) == n


def test_cn_us_sellability_and_lots():
    cn, us = ChinaTradingRules(), USTradingRules()
    assert not cn.can_sell('2024-06-03', '2024-06-03')
    assert us.can_sell('2024-06-03', '2024-06-03')
    assert cn.quantity(173, 'buy', {}) == 100 and us.quantity(173, 'buy', {}) == 173
    assert cn.quantity(199, 'buy', {'board': 'star'}) == 0
    assert cn.quantity(211, 'buy', {'board': 'star'}) == 211
    assert USTradingRules(AccountPolicy(fractional_step=0.01)).quantity(1.237, 'buy', {}) == 1.23
    assert not USTradingRules(AccountPolicy(allow_day_trade=False)).can_sell('2024-06-03', '2024-06-03')


def test_us_ignores_a_share_limits():
    row = {'halted': False, 'raw_open': 10, 'limit_up': 10, 'limit_down': 9}
    assert ChinaTradingRules().rejection(row, 'buy') == 'PRICE_LIMIT'
    assert USTradingRules().rejection(row, 'buy') is None


def test_missing_status_and_limits_are_unknown():
    assert USTradingRules().rejection({}, 'buy') == 'STATUS_UNKNOWN'
    assert ChinaTradingRules().rejection({'halted': False, 'raw_open': 10}, 'buy') == 'PRICE_LIMIT_EVIDENCE_MISSING'


def test_market_specific_research_eligibility_stays_in_rules():
    frame = pd.DataFrame({'instrument_id': ['a', 'b', 'c'], 'is_st': [False, True, None]})
    assert ChinaTradingRules().filter_research_observations(frame).instrument_id.tolist() == ['a']
    assert USTradingRules().filter_research_observations(frame).instrument_id.tolist() == ['a', 'b', 'c']
    with pytest.raises(ContractError, match='ST status unavailable'):
        ChinaTradingRules().filter_research_observations(frame.drop(columns='is_st'))


def test_fee_date_direction_minimum_and_gap():
    fees = DatedFees([FeePeriod('2024-01-01', '2024-05-27', minimum=1), FeePeriod('2024-05-28', '2024-12-31', minimum=2, sell_bps=10)], 'fixture')
    assert fees.charge('2024-05-27', 'buy', 1, 10) == 1
    assert fees.charge('2024-05-28', 'buy', 1, 10) == 2
    assert fees.charge('2024-05-28', 'sell', 1, 10) == 2.01
    with pytest.raises(ContractError):
        fees.charge('2025-01-01', 'buy', 1, 10)


def test_unknown_actions_fail_closed():
    with pytest.raises(ContractError, match='COVERAGE_UNKNOWN'):
        UnknownActions().require_coverage('2020-01-01', '2020-12-31', ['id'])


def test_cash_day_trade_and_unsettled_reinvestment(fixture):
    bars, a = fixture
    e = ExecutionEngine(a, initial_cash=1000)
    row = {'raw_open': 10., 'halted': False}
    security = {}
    assert e.order('fixture:US:10', 'buy', 80, row, pd.Timestamp('2024-05-28'), security) == 80
    assert e.order('fixture:US:10', 'sell', 80, row, pd.Timestamp('2024-05-28'), security) == 80
    assert e.cash > 990 and e.available_cash() < 201
    assert e.order('fixture:US:11', 'buy', 80, row, pd.Timestamp('2024-05-28'), security) < 21
    e.settle(pd.Timestamp('2024-05-29'))
    assert e.available_cash() == e.cash


def test_settlement_bank_holiday_is_separate(fixture):
    e = ExecutionEngine(fixture[1])
    assert e.settlement_date(pd.Timestamp('2024-10-11')) == pd.Timestamp('2024-10-15')
    assert fixture[1].calendar.is_session('2024-10-14')


def test_splits_reverse_splits_preserve_total_basis(fixture):
    _, a = fixture
    e = ExecutionEngine(a)
    for i, day, expected in [(0, '2024-06-03', 200), (1, '2024-07-01', 20)]:
        iid = 'fixture:US:' + str(i)
        e.lots[iid] = [dict(quantity=100., basis=1000., date=pd.Timestamp('2024-01-01'))]
        e.apply_actions(pd.Timestamp(day), pd.DataFrame())
        assert e.quantity(iid) == expected and e.cost_basis(iid) == 1000


def test_dividend_entitlement_and_payment(fixture):
    e = ExecutionEngine(fixture[1])
    e.lots['fixture:US:2'] = [dict(quantity=100., basis=1000., date=pd.Timestamp('2024-01-01'))]
    cash = e.cash
    e.apply_actions(pd.Timestamp('2024-06-14'), pd.DataFrame())
    assert sum(x['amount'] for x in e.receivables) == 20 and e.cash == cash
    e.settle(pd.Timestamp('2024-06-28'))
    assert e.cash == cash + 20 and not e.receivables


def test_merger_and_delisting_accounting(fixture):
    e = ExecutionEngine(fixture[1])
    for i in (4, 5):
        e.lots['fixture:US:' + str(i)] = [dict(quantity=100., basis=1000., date=pd.Timestamp('2024-01-01'))]
    e.apply_actions(pd.Timestamp('2024-08-01'), pd.DataFrame())
    assert e.quantity('fixture:US:4') == 0 and e.cash == e.initial_cash + 1250
    e.apply_actions(pd.Timestamp('2024-08-12'), pd.DataFrame({'raw_open': [20.]}, index=['fixture:US:6']))
    assert e.quantity('fixture:US:5') == 0 and e.quantity('fixture:US:6') == 70
    assert e.cost_basis('fixture:US:6') == pytest.approx(1000 * 1400 / 1650)
    assert e.cash == e.initial_cash + 1500


def test_factor_metadata_and_missing_data_refusal(fixture):
    assert len(FACTOR_METADATA) == 14
    bars, a = fixture
    with pytest.raises(ContractError, match='turnover'):
        compute_instrument_batch(bars.loc[bars.instrument_id.eq('fixture:US:0')].drop(columns='turnover'), a.calendar, 'US')


def test_equivalent_cn_us_inputs_identical_and_batch_invariant(fixture):
    bars, a = fixture
    subset = bars.loc[bars.instrument_id.isin(['fixture:US:10', 'fixture:US:11'])]
    full = compute_instrument_batch(subset, a.calendar, 'US')
    cn = compute_instrument_batch(subset, a.calendar, 'CN')
    pd.testing.assert_frame_equal(full, cn)
    pieces = [compute_instrument_batch(f, a.calendar, 'US') for _, f in subset.groupby('instrument_id')]
    batched = pd.concat(pieces).sort_values(['instrument_id', 'date']).reset_index(drop=True)
    pd.testing.assert_frame_equal(full, batched)
    assert full.loc[full.date < '2023-10-01', 'MOM60'].isna().all()


def test_factor_vintage_leakage_refused(fixture):
    bars, a = fixture
    f = bars.iloc[:1].copy()
    f['available_at'] = pd.Timestamp('2030-01-01', tz='UTC')
    with pytest.raises(ContractError, match='unavailable'):
        compute_instrument_batch(f, a.calendar, 'US')


@pytest.mark.parametrize('years', [10, 20])
def test_arbitrary_long_history_and_warmup(years):
    c = us_calendar('2000-01-01', str(2000 + years) + '-01-01')
    d = c.schedule.index
    assert len(d) > years * 240
    values = np.arange(len(d), dtype=float)
    f = pd.DataFrame(dict(date=d, instrument_id='opaque-id', adj_open=10 + values / 1000, adj_close=10 + values / 1000,
                          volume=10000 + values, amount=1e6 + values, turnover=.01, halted=False, available_at=c.schedule.close.to_numpy()))
    result = compute_instrument_batch(f, c, 'US')
    assert len(result) == len(d) and result.MOM60.iloc[:60].isna().all()
    assert result.target.iloc[-6:].isna().all() and np.isfinite(result.target.iloc[65])


def test_config_rejects_mixed_currency_and_preserves_legacy():
    s = ResearchSpec('US', 'ALL_US_COMMON', '2000-01-01', '2020-01-01', 'SPX_PRICE', 'USD')
    with pytest.raises(ContractError):
        replace(s, currency='CNY')
    from factorlab.config import DataConfig, UniverseConfig
    assert DataConfig(pd.Timestamp('2020-01-01'), pd.Timestamp('2021-01-01')).benchmark == '000905.SH'
    assert UniverseConfig().expected_member_count == 500


def test_cache_resume_rejects_tampering_and_config(tmp_path):
    c = TaskCache(tmp_path, {'source': 'abc'}, {'market': 'US'})
    f = pd.DataFrame({'a': [1, 2]})
    c.put('part', f)
    pd.testing.assert_frame_equal(c.get('part'), f)
    with pytest.raises(ContractError):
        TaskCache(tmp_path, {'source': 'def'}, {'market': 'US'})
    (tmp_path / 'part.parquet').write_bytes(b'corrupt')
    with pytest.raises(ContractError, match='changed'):
        c.get('part')


def test_parquet_csv_projection_and_date_id_pruning(tmp_path):
    f = pd.DataFrame({'date': pd.to_datetime(['2024-01-02', '2024-01-03']), 'instrument_id': ['id1', 'id2'], 'value': [1., 2.]})
    f.to_parquet(tmp_path / 'bars.parquet')
    p = ParquetDataProvider(tmp_path / 'bars.parquet', 'fixture')
    actual = p.read('2024-01-03', '2024-01-03', ['id2'], ['instrument_id', 'value'])
    assert actual.to_dict('list') == {'instrument_id': ['id2'], 'value': [2.]}
    f.rename(columns={'date': 'native_date', 'instrument_id': 'native_id'}).to_csv(tmp_path / 'bars.csv', index=False)
    c = CSVDataProvider(tmp_path / 'bars.csv', {'native_date': 'date', 'native_id': 'instrument_id', 'value': 'value'}, 'fixture')
    result = pd.concat(c.batches('2024-01-03', '2024-01-03', ['id2'], ['value'], 1))
    assert result.value.tolist() == [2.]
