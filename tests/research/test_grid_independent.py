"""Direct independent-sample admission, matrix and bounded reference contracts."""
from dataclasses import replace
import json
from pathlib import Path
import shutil

import pandas as pd
import pytest

from factorlab.experiments import grid_independent as study
from factorlab.experiments.grid_paper import D, run_experiment
from factorlab.trading.contracts import Market
from .test_grid_empirical import snapshot


ROOT = Path(__file__).resolve().parents[2]


def test_frozen_design_and_two_unconditional_cost_matrices():
    prereg, lifecycle, config, rules, hashes = study.load_design(ROOT)
    matrix = study.account_matrix(prereg)
    assert len(matrix) == len(set(matrix)) == 16
    assert [x[0] for x in matrix] == ['base'] * 8 + ['stress'] * 8
    assert {arm for _, arm, _ in matrix} == {'B', 'D', 'cash', 'fixed_budget_buy_hold'}
    assert (config.lower, config.upper, config.levels) == (163.472, 245.208, 8)
    assert lifecycle['expiry_reseed_limit'] == 20
    identity = dict(generation=study.GENERATION, bars_sha256=study.BARS_SHA256)
    ids = [study.account_identity(hashes, identity, *entry) for entry in matrix]
    assert len(set(ids)) == 16
    assert ids == [study.account_identity(hashes, identity, *entry) for entry in matrix]


def test_preregistration_and_source_cannot_silently_change(tmp_path):
    _, _, _, _, hashes = study.load_design(ROOT)
    paths = list(hashes) + ['docs/validation/p7-r04a/preregistration-lock.json',
                           'docs/validation/p7-r04a/verified-assets.json']
    for relative in paths:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    for path in (tmp_path / 'configs').glob('grid*.json'):
        path.write_bytes(path.read_bytes().replace(b'\r\n', b'\n'))
    study.load_design(tmp_path)
    path = tmp_path / 'configs/grid_independent_sample_preregistration.json'
    path.write_text(path.read_text().replace('2160', '2159'), encoding='utf-8')
    with pytest.raises(study.StudyBlocked, match='frozen content digest'):
        study.load_design(tmp_path)
    shutil.copyfile(ROOT / 'configs/grid_independent_sample_preregistration.json', path)
    path = tmp_path / 'configs/grid_paper_nvda_hourly.json'
    path.write_text(path.read_text().replace('163.472', '160'), encoding='utf-8')
    with pytest.raises(study.StudyBlocked, match='frozen content digest'):
        study.load_design(tmp_path)


def test_snapshot_rejects_generation_and_actual_content_hash(tmp_path, monkeypatch):
    with pytest.raises(study.StudyBlocked, match='generation'):
        study.load_sample(tmp_path)
    directory = tmp_path / study.GENERATION
    directory.mkdir()
    snapshot(directory)
    manifest_path = directory / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    monkeypatch.setattr(study, 'MANIFEST_SHA256', study.digest(manifest_path.read_bytes()))
    monkeypatch.setattr(study, 'VERSION', manifest['data_version'])
    monkeypatch.setattr(study, 'BARS_SHA256', manifest['tables']['bars']['sha256'])
    with (directory / 'bars.parquet').open('ab') as stream:
        stream.write(b'corruption')
    with pytest.raises(ValueError, match='bars digest mismatch'):
        study.load_sample(directory)


def test_unresolved_listing_allows_exploratory_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(study, 'load_sample', lambda _: ([], {}))
    executed = []
    def execute(output, **kwargs):
        output.mkdir()
        executed.append(output)
        return 'executed'
    monkeypatch.setattr(study, 'execute_matrix', execute)
    output = tmp_path / 'exploratory'
    assert study.run_study(output, snapshot='unused', repository=ROOT) == 'executed'
    assert executed == [output]
    admission = json.loads((output / 'admission.json').read_text())
    original = json.loads((ROOT / 'docs/validation/p7-r04b-e01/admission.json').read_text())
    assert admission['listing_cross_check'] == original['listing_cross_check']
    assert admission['listing_cross_check']['status'] == 'UNRESOLVED'
    assert admission['execution_basis'] == study.RESEARCH_LEVEL
    assert admission['preregistered_listing_condition_relaxed'] is True
    assert admission['strict_confirmatory_contract'] is False


def test_independence_and_existing_receipt_integrity_still_required(tmp_path):
    path = tmp_path / 'docs/validation/p7-r04b-e01/admission.json'
    path.parent.mkdir(parents=True)
    admission = json.loads((ROOT / 'docs/validation/p7-r04b-e01/admission.json').read_text())
    admission['independence']['not_used_for_parameter_development_or_profit_selection'] = False
    path.write_text(json.dumps(admission))
    with pytest.raises(study.StudyBlocked, match='provenance declaration'):
        study.require_admission(tmp_path)
    admission['independence']['not_used_for_parameter_development_or_profit_selection'] = True
    receipt = tmp_path / 'receipt.txt'
    receipt.write_text('actual evidence')
    admission['listing_cross_check']['receipts'] = [dict(path='receipt.txt', sha256='incorrect')]
    path.write_text(json.dumps(admission))
    with pytest.raises(study.StudyBlocked, match='receipt digest mismatch'):
        study.require_admission(tmp_path)


def hourly_bars(prices, volume=100):
    return [dict(id=f'fixture-{i}', time=time.isoformat(), price=price, close=price,
                 high=price+1, low=price-1, volume=volume, completed_bar=True)
            for i, (time, price) in enumerate(zip(
                pd.date_range('2026-01-01', periods=len(prices), freq='h', tz='UTC'), prices))]


def run_reference(directory, prices, *, capital=1000, volume=100):
    _, _, config, rules, _ = study.load_design(ROOT)
    run_experiment(directory, bars=hourly_bars(prices, volume), config=config, rules=rules,
        capital=capital, fee_rate=D('.0004'), slippage=D('.0006'), participation=D('.01'),
        market=Market.BINANCE_USDM, instrument_id='BINANCE:USDM:NVDAUSDT', interval='1h',
        buy_hold=True, reference_budget=dict(max_inventory='1', max_order='.1'))
    return json.loads((directory / 'result.json').read_text()), pd.read_csv(directory / 'daily.csv')


def test_common_budget_reference_uses_first_closed_price_not_future(tmp_path):
    a, nav = run_reference(tmp_path / 'a', [200, 199, 198, 205])
    b, _ = run_reference(tmp_path / 'b', [200, 1500, 3000, 5000])
    for result in (a, b):
        quantities = [D(order['intent']['quantity']) for order in result['orders'].values()]
        assert len(quantities) == 10
        assert max(quantities) <= D('.1')
        assert sum(quantities) == D(1)
    assert nav.inventory.max() <= 1
    assert nav.inventory.iloc[0] == 0
    assert nav.inventory.iloc[1] == 1
    fills = pd.read_csv(tmp_path / 'a/trades.csv')
    assert len(fills) == 10
    assert fills.execution_date.eq('2026-01-01T01:00:00+00:00').all()


def test_reference_low_budget_rounding_and_partial_shared_volume(tmp_path):
    result, nav = run_reference(tmp_path / 'budget', [200, 199, 198], capital=150)
    q = (min(D(1), D(150)/(D(200)*D('1.0006')*D('1.0004'))) // D('.01')) * D('.01')
    assert sum(D(order['intent']['quantity']) for order in result['orders'].values()) == q
    assert nav.committed.max() <= 150
    _, partial = run_reference(tmp_path / 'partial', [200, 199, 198, 197], volume=5)
    assert partial.inventory.tolist() == pytest.approx([0, .05, .1, .15])


@pytest.mark.parametrize('target,maximum,step,capital,legal', [
    ('.11', '.10', '.01', 100, True),
    ('1', '.10', '.01', 23, True),
    ('.10', '.10', '.01', 100, True),
    ('.02', '.10', '.01', 100, False),
    ('.11', '.02', '.01', 100, False),
    ('.05', '.04', '.01', 100, False),
])
def test_reference_exact_legal_partition(tmp_path, target, maximum, step, capital, legal):
    tmp_path = tmp_path / 'reference'
    _, _, config, rules, _ = study.load_design(ROOT)
    rules = replace(rules, step_size=D(step), min_quantity=D('.01'), min_notional=D(5))
    kwargs = dict(bars=hourly_bars([200, 200]), config=config, rules=rules,
        capital=capital, fee_rate=D('.0004'), slippage=D('.0006'), participation=D('.01'),
        market=Market.BINANCE_USDM, instrument_id='BINANCE:USDM:NVDAUSDT', interval='1h',
        buy_hold=True, reference_budget=dict(max_inventory=target, max_order=maximum))
    if not legal:
        with pytest.raises(ValueError, match='bounded reference'):
            run_experiment(tmp_path, **kwargs)
        return
    run_experiment(tmp_path, **kwargs)
    result = json.loads((tmp_path / 'result.json').read_text())
    intents = [order['intent'] for order in result['orders'].values()]
    quantities = [D(intent['quantity']) for intent in intents]
    expected = min(D(target), D(capital) / (D(200) * D('1.0006') * D('1.0004')))
    expected = expected // D(step) * D(step)
    assert sum(quantities, D(0)) == expected
    assert all(q % D(step) == 0 and rules.min_quantity <= q <= D(maximum) for q in quantities)
    assert all(D(intent['quantity']) * D(intent['limit_price']) >= D(5) for intent in intents)
    assert sum(D(intent['quantity']) * D(intent['limit_price']) * D('1.0004')
               for intent in intents) <= D(capital)
    assert pd.read_csv(tmp_path / 'daily.csv').committed.max() <= capital


def test_all_sixteen_synthetic_accounts_serial_parallel_equal(tmp_path):
    prereg, lifecycle, config, rules, hashes = study.load_design(ROOT)
    bars = hourly_bars([200, 189, 201, 190, 202, 188, 203, 199])
    identity = dict(generation='synthetic-only', bars_sha256=study.digest(b'fixture'),
                    instrument_id='BINANCE:USDM:NVDAUSDT')
    kwargs = dict(bars=bars, identity=identity, prereg=prereg, lifecycle=lifecycle,
                  config=replace(config, atr_window=2), rules=rules, hashes=hashes)
    first = study.execute_matrix(tmp_path / 'one', max_workers=1, **kwargs)
    second = study.execute_matrix(tmp_path / 'two', **kwargs)
    expected = study.account_matrix(prereg)
    for table in (first, second):
        assert list(table[['scenario', 'arm', 'mode']].itertuples(index=False, name=None)) == expected
    assert json.loads((tmp_path / 'two/study.json').read_text())['max_workers'] == 4
    pd.testing.assert_frame_equal(
        pd.read_csv(tmp_path / 'one/summary.csv').drop(columns='wall_seconds'),
        pd.read_csv(tmp_path / 'two/summary.csv').drop(columns='wall_seconds'))
    pd.testing.assert_frame_equal(first.drop(columns='wall_seconds'), second.drop(columns='wall_seconds'))
    assert len(first) == 16
    assert first.ledger_reconciled.all()
    assert len(set(first.account_id)) == 16
    for scenario, arm, mode in study.account_matrix(prereg):
        relative = Path(scenario) / arm / mode
        for filename in ('daily.csv', 'trades.csv'):
            pd.testing.assert_frame_equal(pd.read_csv(tmp_path / 'one' / relative / filename),
                                          pd.read_csv(tmp_path / 'two' / relative / filename))
        assert json.loads((tmp_path / 'one' / relative / 'fifo.json').read_text()) == json.loads(
            (tmp_path / 'two' / relative / 'fifo.json').read_text())
        for run in ('one', 'two'):
            assert (tmp_path / run / relative / 'paper.sqlite').is_file()
    assert len(list((tmp_path / 'two').rglob('paper.sqlite'))) == 16
