"""Scenario reaches the existing pipeline and immutable experiment bindings."""
from dataclasses import replace
from pathlib import Path

from examples.research.research_pipeline_synthetic import configured
from factorlab.application.research_pipeline import ResearchPipeline
from factorlab.backtest.capacity import ResearchExecutionScenario
from factorlab.experiments import ExperimentTracker


def test_optional_capacity_configuration_and_missing_liquidity_are_bound(tmp_path):
    config, hub, _ = configured(tmp_path / 'output', models=['factor_score'])
    config = replace(config, research_scenario=ResearchExecutionScenario(participation_limit=.1),
                     execution_mode='incremental_feedback')
    tracker = ExperimentTracker(tmp_path / 'tracker')
    result = ResearchPipeline(config).run(hub, tracker=tracker,
        repo_root=Path(__file__).resolve().parents[2], command=['pytest', 'capacity-pipeline'])
    execution = result.executions['factor_score']
    assert execution.trades.empty
    assert set(execution.rejections.reason) == {'liquidity_NOT_COMPUTABLE'}
    assert (execution.nav.cash == config.execution.initial_cash).all()
    definition = tracker.get_experiment(result.run_ids[0])['payload']
    assert definition['costs']['research_scenario']['participation_limit'] == .1
    assert definition['costs']['research_source']
