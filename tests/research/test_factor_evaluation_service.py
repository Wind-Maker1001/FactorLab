"""Offline contract tests for the CLI's admitted factor-evaluation orchestration."""
from types import SimpleNamespace

import pandas as pd
import pytest

from factorlab import cli
from factorlab.errors import FactorLabError


def _inputs(tmp_path, regime=True):
    dates = pd.bdate_range('2024-01-02', periods=3)
    frame = pd.DataFrame({'date': dates, 'symbol': ['A', 'A', 'A'], 'F1': [1., 2., 3.]})
    config = SimpleNamespace(
        factors=('F1',), label=SimpleNamespace(holding_days=7),
        evaluation=SimpleNamespace(quantiles=5, min_cross_section=20),
        market_regime=SimpleNamespace(enabled=regime),
        paths=SimpleNamespace(factor_results_dir=tmp_path / 'report'),
    )
    events = []
    payload = {}

    def add_output(name, path):
        events.append(('output', name, path, dict(payload)))

    run = SimpleNamespace(payload=payload, add_output=add_output)
    return config, run, frame, dates, events


def _wire_cli(monkeypatch, config, run, frame, dates, events):
    benchmark = object()

    def load_benchmark(current):
        assert current is config
        events.append('benchmark')
        return benchmark

    def from_bars(bars):
        assert bars is benchmark
        events.append('full_calendar')
        return SimpleNamespace(dates=dates)

    def research_calendar(current, full_calendar):
        assert current is config and full_calendar is dates
        events.append('research_calendar')
        return dates

    def verified(current, calendar, receipt, *, full_calendar):
        assert current is config and receipt is run
        assert calendar is dates and full_calendar is dates
        events.append('verified')
        return frame, 'dataset-sha', 'universe-sha'

    monkeypatch.setattr(cli, '_load_benchmark_with_warmup', load_benchmark)
    monkeypatch.setattr(cli.TradingCalendar, 'from_benchmark_bars', from_bars)
    monkeypatch.setattr(cli, '_research_calendar', research_calendar)
    monkeypatch.setattr(cli, '_load_verified_dataset', verified)


@pytest.mark.parametrize('regime', [True, False])
@pytest.mark.parametrize('rank_ic', [[0.2, None, -0.1], [None, None, None]])
def test_cli_evaluation_preserves_parameters_receipts_and_write_before_rejection(
    tmp_path, monkeypatch, regime, rank_ic,
):
    config, run, frame, dates, events = _inputs(tmp_path, regime)
    original = frame.copy(deep=True)
    _wire_cli(monkeypatch, config, run, frame, dates, events)
    result = SimpleNamespace(daily_ic=pd.DataFrame({'RankIC': rank_ic}))
    summary = config.paths.factor_results_dir / 'summary.md'

    def evaluate(data, factors, **kwargs):
        assert data is frame and factors is config.factors
        assert run.payload == {'dataset_sha256': 'dataset-sha', 'universe_version': 'universe-sha'}
        assert kwargs == dict(target='EXCESS_RET7', quantiles=5, minimum_cross_section=20,
                              group_column='MARKET_REGIME' if regime else None, calendar=dates)
        events.append('evaluate')
        return result

    def write_report(evaluation, destination):
        assert evaluation is result and destination is config.paths.factor_results_dir
        assert 'valid_rank_ic_observations' not in run.payload
        events.append('write_report')
        return summary

    monkeypatch.setattr(cli, 'evaluate_factors', evaluate)
    monkeypatch.setattr(cli, 'write_factor_report', write_report)
    count = sum(pd.notna(rank_ic))
    if count:
        cli.command_evaluate(config, run)
    else:
        with pytest.raises(FactorLabError, match='^Factor evaluation produced no valid RankIC observations$'):
            cli.command_evaluate(config, run)
    assert events[:6] == ['benchmark', 'full_calendar', 'research_calendar', 'verified', 'evaluate', 'write_report']
    assert events[6] == ('output', 'factor_report', summary,
                         {'dataset_sha256': 'dataset-sha', 'universe_version': 'universe-sha'})
    assert run.payload['valid_rank_ic_observations'] == count
    pd.testing.assert_frame_equal(frame, original, check_exact=True)


@pytest.mark.parametrize('stage', ['benchmark', 'full_calendar', 'research_calendar', 'verified'])
def test_upstream_rejection_never_reaches_evaluation(tmp_path, monkeypatch, stage):
    config, run, frame, dates, events = _inputs(tmp_path)
    _wire_cli(monkeypatch, config, run, frame, dates, events)
    error = FactorLabError('rejected ' + stage)

    def reject(*args, **kwargs):
        raise error

    if stage == 'full_calendar':
        monkeypatch.setattr(cli.TradingCalendar, 'from_benchmark_bars', reject)
    else:
        target = {'benchmark': '_load_benchmark_with_warmup', 'research_calendar': '_research_calendar',
                  'verified': '_load_verified_dataset'}[stage]
        monkeypatch.setattr(cli, target, reject)

    def forbidden(*args, **kwargs):
        pytest.fail('evaluation/report called after input rejection')

    monkeypatch.setattr(cli, 'evaluate_factors', forbidden)
    monkeypatch.setattr(cli, 'write_factor_report', forbidden)
    with pytest.raises(FactorLabError) as caught:
        cli.command_evaluate(config, run)
    assert caught.value is error
    assert run.payload == {}


@pytest.mark.parametrize('stage', ['target', 'evaluate', 'report'])
def test_downstream_exceptions_keep_identity_and_partial_receipt(tmp_path, monkeypatch, stage):
    config, run, frame, dates, events = _inputs(tmp_path)
    _wire_cli(monkeypatch, config, run, frame, dates, events)
    error = OSError('failed ' + stage)

    def reject(*args, **kwargs):
        raise error

    def evaluate(*args, **kwargs):
        events.append('evaluate')
        return SimpleNamespace(daily_ic=pd.DataFrame({'RankIC': [0.1]}))

    def write_report(*args, **kwargs):
        events.append('report')
        return tmp_path / 'summary.md'

    monkeypatch.setattr(cli, 'evaluate_factors', reject if stage == 'evaluate' else evaluate)
    monkeypatch.setattr(cli, 'write_factor_report', reject if stage == 'report' else write_report)
    if stage == 'target':
        monkeypatch.setattr(cli, '_target_column', reject)
    with pytest.raises(OSError) as caught:
        cli.command_evaluate(config, run)
    assert caught.value is error
    assert run.payload == {'dataset_sha256': 'dataset-sha', 'universe_version': 'universe-sha'}
    assert not any(isinstance(event, tuple) and event[0] == 'output' for event in events)
    assert ('evaluate' in events) == (stage == 'report')


def test_verified_rejection_does_not_enter_application_service(tmp_path, monkeypatch):
    config, run, frame, dates, events = _inputs(tmp_path)
    _wire_cli(monkeypatch, config, run, frame, dates, events)
    error = FactorLabError('unverified dataset')

    def reject(*args, **kwargs):
        raise error

    def forbidden(*args, **kwargs):
        pytest.fail('application service entered before successful validation')

    monkeypatch.setattr(cli, '_load_verified_dataset', reject)
    monkeypatch.setattr(cli, 'evaluate_admitted_dataset', forbidden)
    with pytest.raises(FactorLabError) as caught:
        cli.command_evaluate(config, run)
    assert caught.value is error
    assert run.payload == {}


def test_service_accepts_custom_dependencies_without_cli_import(tmp_path):
    import os
    from pathlib import Path
    import subprocess
    import sys

    source = Path(__file__).resolve().parents[2] / 'src'
    script = """
import sys
from types import SimpleNamespace
import pandas as pd
from factorlab.application.factor_evaluation import evaluate_admitted_dataset
assert 'factorlab.cli' not in sys.modules
frame = pd.DataFrame({'custom': [1., 2.]})
config = SimpleNamespace(factors=('custom',), evaluation=SimpleNamespace(quantiles=2, min_cross_section=2),
    market_regime=SimpleNamespace(enabled=False), paths=SimpleNamespace(factor_results_dir='destination'))
outputs = []
run = SimpleNamespace(payload={}, add_output=lambda *args: outputs.append(args))
seen = []
def evaluate(data, factors, **kwargs):
    assert data is frame and factors == ('custom',)
    assert kwargs['target'] == 'CUSTOM_TARGET' and kwargs['calendar'] == 'frozen-calendar'
    return SimpleNamespace(daily_ic=pd.DataFrame({'RankIC': [0.4, None]}))
def write_report(result, destination):
    assert destination == 'destination'
    return 'custom-summary'
evaluate_admitted_dataset(config, run, frame, research_calendar='frozen-calendar', dataset_sha256='hash',
    universe_version='version', evaluate=evaluate, write_report=write_report,
    target_for_config=lambda c: 'CUSTOM_TARGET', logger=SimpleNamespace(info=lambda *a: seen.append(a)))
assert outputs == [('factor_report', 'custom-summary')]
assert run.payload == {'dataset_sha256': 'hash', 'universe_version': 'version', 'valid_rank_ic_observations': 1}
assert len(seen) == 1 and 'factorlab.cli' not in sys.modules
"""
    result = subprocess.run([sys.executable, '-B', '-c', script], cwd=tmp_path,
                            env=dict(os.environ, PYTHONPATH=str(source)), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
