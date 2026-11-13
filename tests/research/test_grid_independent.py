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


def test_unresolved_listing_blocks_before_any_account_or_output(tmp_path, monkeypatch):
    monkeypatch.setattr(study, 'load_sample', lambda _: ([], {}))
    def forbidden(*args, **kwargs):
        pytest.fail('an account ran before research admission')
    monkeypatch.setattr(study, 'execute_matrix', forbidden)
    with pytest.raises(study.StudyBlocked, match='cross-check unresolved'):
        study.run_study(tmp_path / 'no-output', snapshot='unused', repository=ROOT)
    assert not (tmp_path / 'no-output').exists()


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


def test_all_sixteen_synthetic_accounts_repeat_exactly(tmp_path):
    prereg, lifecycle, config, rules, hashes = study.load_design(ROOT)
    bars = hourly_bars([200, 189, 201, 190, 202, 188, 203, 199])
    identity = dict(generation='synthetic-only', bars_sha256=study.digest(b'fixture'),
                    instrument_id='BINANCE:USDM:NVDAUSDT')
    kwargs = dict(bars=bars, identity=identity, prereg=prereg, lifecycle=lifecycle,
                  config=replace(config, atr_window=2), rules=rules, hashes=hashes)
    first = study.execute_matrix(tmp_path / 'one', **kwargs)
    second = study.execute_matrix(tmp_path / 'two', **kwargs)
    pd.testing.assert_frame_equal(first.drop(columns='wall_seconds'), second.drop(columns='wall_seconds'))
    assert len(first) == 16
    assert first.ledger_reconciled.all()
    assert len(set(first.account_id)) == 16
    for scenario, arm, mode in study.account_matrix(prereg):
        relative = Path(scenario) / arm / mode
        for filename in ('daily.csv', 'trades.csv'):
            pd.testing.assert_frame_equal(pd.read_csv(tmp_path / 'one' / relative / filename),
                                          pd.read_csv(tmp_path / 'two' / relative / filename))
