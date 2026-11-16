from dataclasses import replace
from datetime import timedelta
import csv
import socket

import pytest

from factorlab.experiments.perp_risk import START, synthetic_rule, price_frames, paper_frames, run, monte_carlo_frames
from factorlab.research.perp_risk import D, Funding, Tier, evaluate


def assess(prices=('100','100','90'), *, rates=(), quantity='10', capital='1000', mode='CROSS', isolated=None, rule=None):
    rule = rule or synthetic_rule()
    frames = price_frames(prices, rule=rule, budget=D(capital), contracts=D(quantity))
    funding = tuple(Funding(str(i), frames[i].time, rule.symbol, D(rate),
        frames[i].positions[0].mark_price, 'ASSUMED_SETTLEMENT', 'fixture') for i, rate in enumerate(rates, 1))
    return evaluate(frames=frames, rules={rule.symbol:rule}, funding=funding, mode=mode,
        budget=D(capital), order_margin=D(0), portfolio_complete=True,
        equity_basis='SYNTHETIC_WALLET', isolated_collateral=D(isolated) if isolated else None)


def test_unlevered_and_conservation():
    result = assess(rule=synthetic_rule('1'))
    final = result['rows'][-1]
    assert final['equity'] == D(900)
    assert final['unrealized_pnl'] == D(-100)
    assert final['initial_margin'] == D(900)
    assert final['available_margin'] == 0
    for row in result['rows']:
        assert row['equity'] == row['original_equity'] + row['valuation_adjustment'] + row['cumulative_funding']
    assert result['authoritative_liquidation'] == 'BLOCKED_RULE_EVIDENCE'


@pytest.mark.parametrize('quantity,rate,expected', [('10','.01','-20'), ('10','-.01','20'), ('-10','.01','20'), ('-10','-.01','-20'), ('0','.01','0')])
def test_multiple_funding(quantity, rate, expected):
    result = assess(('100','100','100'), rates=(rate,rate), quantity=quantity)
    assert result['rows'][-1]['cumulative_funding'] == D(expected)
    assert sum(e['cashflow'] for e in result['settlements']) == D(expected)
    assert result['rows'][-1]['equity'] == D(1000)+D(expected)


def test_crash_not_clamped_or_executed():
    result = assess(('100','85','79'), quantity='50', rates=('.01','.01'))
    last = result['rows'][-1]
    assert last['equity'] == D('-132')
    assert last['available_margin'] == D('-527')
    assert last['margin_utilization'] is None
    assert 'SCENARIO_LIQUIDATION_THRESHOLD' in last['events']
    assert 'NEGATIVE_ACCOUNT_EQUITY' in last['events']
    assert last['notional'] == D(3950)


def test_isolation():
    isolated = assess(('100','90','80'), mode='ISOLATED', isolated='100')['rows'][-1]
    cross = assess(('100','90','80'))['rows'][-1]
    assert isolated['equity'] == cross['equity'] == 800
    assert isolated['risk_equity'] == -100
    assert isolated['external_balance'] == 900
    assert 'SCENARIO_LIQUIDATION_THRESHOLD' not in cross['events']


def test_threshold_equal_and_tier_boundary():
    result = assess(('100','100'), capital='50', quantity='10')
    assert 'SCENARIO_LIQUIDATION_THRESHOLD' in result['rows'][0]['events']
    rule = replace(synthetic_rule(), tiers=(Tier(D(0),D(1000),D('.1'),D('.05'),D(0)),
        Tier(D(1000),D(2000),D('.2'),D('.1'),D(50))))
    assert rule.margin(D(1000)) == (D(200),D(50))
    with pytest.raises(ValueError, match='outside'):
        rule.margin(D(2000))


def test_input_rejections():
    rule = synthetic_rule()
    frames = price_frames(['100','90'], rule=rule, budget=D(1000), contracts=D(10))
    kwargs = dict(frames=frames, rules={rule.symbol:rule}, mode='CROSS', budget=D(1000),
                  order_margin=D(0), portfolio_complete=True, equity_basis='SYNTHETIC_WALLET')
    event = Funding('one', frames[1].time, rule.symbol,D('.01'),D(90),'ASSUMED_SETTLEMENT','fixture')
    with pytest.raises(ValueError, match='duplicate'):
        evaluate(**kwargs, funding=(event,event))
    with pytest.raises(ValueError, match='exact observation'):
        evaluate(**kwargs, funding=(replace(event,time=event.time+timedelta(seconds=1)),))
    with pytest.raises(ValueError, match='complete portfolio'):
        evaluate(**(kwargs | {'portfolio_complete':False}))
    with pytest.raises(ValueError, match='strictly increasing'):
        evaluate(**(kwargs | {'frames':frames[::-1]}))
    with pytest.raises(ValueError):
        replace(rule, version='unknown')
    with pytest.raises(ValueError):
        replace(event, mark=D('NaN'))


def test_cross_portfolio():
    rule = synthetic_rule()
    second = replace(rule, symbol='SECONDUSDT')
    from factorlab.research.perp_risk import Frame
    positions = (rule.position(D(10),D(100),D(90)), second.position(D(-10),D(100),D(90)))
    result = evaluate(frames=[Frame(START,positions,D(1000))], rules={rule.symbol:rule,second.symbol:second},
        mode='CROSS',budget=D(1000),order_margin=D(5),portfolio_complete=True,equity_basis='SYNTHETIC_WALLET')
    row = result['rows'][0]
    assert row['unrealized_pnl'] == 0
    assert row['initial_margin'] == 180
    assert row['available_margin'] == 815


def test_paper_bridge_does_not_double_count_costs(tmp_path):
    daily = tmp_path/'daily.csv'
    with daily.open('w', newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=['date','close','inventory','unrealized','equity'])
        writer.writeheader()
        writer.writerow(dict(date=START.isoformat(),close='100',inventory='10',unrealized='20',equity='1018'))
    content = daily.read_bytes()
    time = START+timedelta(hours=1,microseconds=-1)
    frames = paper_frames(tmp_path,symbol='SYNTHUSDT',mark_prices={time:D(90)})
    rule = synthetic_rule()
    result = evaluate(frames=frames,rules={rule.symbol:rule},mode='CROSS',budget=D(1000),
        order_margin=D(0),portfolio_complete=True,equity_basis='PAPER_NAV')
    assert result['rows'][0]['equity'] == 918
    assert result['rows'][0]['valuation_adjustment'] == -100
    assert daily.read_bytes() == content


def test_repeatable_offline_delivery(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    first = run(tmp_path/'first')
    assert first == run(tmp_path/'second')
    assert (tmp_path/'first/results.json').read_bytes() == (tmp_path/'second/results.json').read_bytes()
    assert len(first) == 7


def test_contract_units_and_settlement_mark_are_independent():
    rule = replace(synthetic_rule(), contract_unit_base=D('.1'))
    frames = price_frames(['100','90'],rule=rule,budget=D(1000),contracts=D(100))
    event = Funding('separate-mark',frames[1].time,rule.symbol,D('.01'),D(80),'ASSUMED_SETTLEMENT','fixture')
    result = evaluate(frames=frames,rules={rule.symbol:rule},funding=[event],mode='CROSS',
        budget=D(1000),order_margin=D(0),portfolio_complete=True,equity_basis='SYNTHETIC_WALLET')
    assert result['rows'][-1]['unrealized_pnl'] == -100
    assert result['rows'][-1]['funding'] == -8
    assert result['rows'][-1]['equity'] == 892


def test_isolated_dynamic_exposure_rejected():
    rule = synthetic_rule()
    frames = price_frames(['100','90'],rule=rule,budget=D(1000),contracts=D(10))
    frames = (frames[0], replace(frames[1], positions=(replace(frames[1].positions[0],quantity=D(9)),)))
    with pytest.raises(ValueError, match='fixed endowed'):
        evaluate(frames=frames,rules={rule.symbol:rule},mode='ISOLATED',budget=D(1000),
            order_margin=D(0),portfolio_complete=True,equity_basis='SYNTHETIC_WALLET',isolated_collateral=D(100))


def test_mc_asset_path_integration():
    from factorlab.research.monte_carlo import MonteCarloConfig, MonteCarloInput, MonteCarloRunner
    result = MonteCarloRunner(MonteCarloConfig(n_paths=2,n_steps=2,seed=4,save_full_paths=True)).run(
        MonteCarloInput(weights=[1.],drift=[0.],volatility=[.01],initial_prices=[100.]))
    frames = monte_carlo_frames(result,path_id=0,asset_id=0,rule=synthetic_rule(),budget=D(1000),
        contracts=D(10),start=START,step=timedelta(hours=1))
    assert len(frames) == 3
    assert frames[0].original_equity == 1000
