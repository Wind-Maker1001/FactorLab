"""Publish aggregate P9-D02 research receipts without vendor rows or full ledgers."""
import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from factorlab.data.market_evidence_sidecar import POLICY, SAFETY
from factorlab.dataset.identity import file_hash


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def publish_nav(study, output):
    nav = pd.read_csv(study / 'daily-nav.csv')
    nav = nav.loc[nav.scope.eq('FULL_SAVED_FOLD0'),
        ['date', 'model_id', 'scope', 'strategy_nav', 'portfolio_value']].copy()
    nav['valuation_basis'] = 'CONTAINS_MODEL_VALUATION_ASSUMPTION'
    nav['valuation_policy'] = POLICY
    marks = pd.read_csv(study / 'valuations.csv')
    assumed = marks.loc[marks.scope.eq('FULL_SAVED_FOLD0') & marks.valuation_status.eq('MODEL_VALUATION_ASSUMPTION'), ['model_id', 'date']].drop_duplicates()
    nav['session_uses_assumption'] = pd.MultiIndex.from_frame(nav[['model_id', 'date']]).isin(pd.MultiIndex.from_frame(assumed))
    nav['performance_eligibility'] = 'CONDITIONAL_RAW_PRICE_RESEARCH_ONLY'
    nav.to_csv(output / 'conditional-daily-nav.csv', index=False)


def publish(study, original_study, output):
    result = json.loads((study / 'result.json').read_bytes())
    valuations = pd.read_csv(study / 'valuations.csv')
    nav = pd.read_csv(study / 'daily-nav.csv')
    comparison = pd.read_csv(study / 'comparison.csv')
    output.mkdir(parents=True, exist_ok=False)
    scopes, reconciliation, affected = [], [], []
    for account in result['accounts']:
        model = account['model_id']
        if account['status'] == 'BLOCKED':
            scopes.append(dict(model_id=model, fold=0, scope='MINIMUM_REPLAY', status='BLOCKED',
                start=result['first_date'], end=result['last_date'], sessions=0, newly_qualified_sessions=0,
                assumption_account_days=0, net_return=None, sharpe=None))
            continue
        account_name = model + '-fold-0'
        for field, keys in [('positions', ['date', 'symbol']), ('nav', ['date'])]:
            replay = pd.read_parquet(study / account_name / (field + '.parquet'))
            saved = pd.read_parquet(original_study / account_name / (field + '.parquet'))
            saved = saved.loc[saved.date.le(pd.Timestamp(result['last_date']))]
            pd.testing.assert_frame_equal(replay.sort_values(keys).reset_index(drop=True),
                saved.sort_values(keys).reset_index(drop=True), check_exact=True)
        for scope in ('MINIMUM_REPLAY', 'FULL_SAVED_FOLD0'):
            key = scope + '/' + POLICY
            if key not in account['audits']:
                continue
            part = nav.loc[nav.model_id.eq(model) & nav.scope.eq(scope)]
            marks = valuations.loc[valuations.model_id.eq(model) & valuations.scope.eq(scope)
                & valuations.valuation_policy.eq(POLICY)]
            assumed = marks.loc[marks.valuation_status.eq('MODEL_VALUATION_ASSUMPTION')]
            first_gap = assumed.date.min()
            count = len(part)
            restored = int(part.date.ge(first_gap).sum())
            scopes.append(dict(model_id=model, fold=0, scope=scope, status='CONDITIONAL_VALUATION_RESEARCH',
                start=result['first_date'], end=part.date.max(), sessions=count + 1,
                newly_qualified_sessions=restored, assumption_account_days=int(assumed.date.nunique()),
                net_return=None, sharpe=None))
            reconciliation.append(dict(model_id=model, fold=0, scope=scope,
                **account['audits'][key], nav_change_from_saved_marks=0.0,
                original_raw_close_unchanged=True, virtual_suspension_trades=0))
            if scope == 'FULL_SAVED_FOLD0':
                affected.extend(assumed[['model_id', 'date', 'valuation_status']].to_dict('records'))
    for account in result['remaining_accounts']:
        scopes.append(dict(model_id=account['model_id'], fold=account['fold'], scope='FULL_FOLD',
            status='NOT_RUN', start=None, end=None, sessions=0, newly_qualified_sessions=0,
            assumption_account_days=0, net_return=None, sharpe=None))
    blockers = [dict(model_id='B_lightgbm', fold=0, date='2023-05-08', security='000540.SZ',
        field='trade_status', status='UNKNOWN', reason='NO_SIGNAL_TIME_EXECUTION_PERMISSION',
        order_executability='ORDER_EXECUTABLE_UNVERIFIED')]
    pd.DataFrame(scopes).to_csv(output / 'restored-scope.csv', index=False)
    pd.DataFrame(reconciliation).to_csv(output / 'account-reconciliation.csv', index=False)
    pd.DataFrame(blockers).to_csv(output / 'remaining-blockers.csv', index=False)
    identities = {name: file_hash(study / name) for name in ('config.json', 'result.json', 'comparison.csv', 'valuations.csv', 'daily-nav.csv')}
    dump(output / 'evidence-consumption.json', dict(schema=result['schema'],
        source_manifest_sha256=result['source_manifest_sha256'], evidence_shards=result['evidence_shards'],
        source_shards=result['source_shards'], candidate_prediction_sha256=result['candidate_prediction_sha256'],
        original_predictions_unchanged=True, target_comparison=comparison.to_dict('records'),
        private_artifact_sha256=identities, safety=list(SAFETY)))
    dump(output / 'valuation-policy.json', dict(policy=POLICY, version='1', explicitly_selected=True,
        raw_close_patched=False, positive_existing_holdings_only=True, nontradable=True,
        required_evidence=['ISSUER_CONFIRMED_SUSPENSION', 'OBSERVED_RAW_CLOSE'],
        statuses=['OBSERVED_MARKET_PRICE', 'MODEL_VALUATION_ASSUMPTION', 'UNVALUABLE_POSITION'],
        assumption_account_dates=affected, strategy_nav_contains_valuation_assumptions=True,
        nav_change_from_saved_marks=0.0, safety=list(SAFETY)))
    dump(output / 'state-consumption.json', dict(result['state_consumption'],
        other_unknown_dates='UNKNOWN', global_enum_mapping_changed=False,
        source_version='P9_D01_MARKET_EVIDENCE_V1', valid_security_date_field=['000540.SZ', '2023-05-08', 'is_st'],
        execution_state_evidence_not_authorized=True))
    dump(output / 'results.json', dict(result,
        restored_scope=scopes, account_reconciliation=reconciliation,
        remaining_blockers=blockers, full_three_fold_net_return=None, full_three_fold_sharpe=None,
        raw_price_conditional_research_only=True, virtual_suspension_trades=0))
    publish_nav(study, output)
    print(json.dumps(dict(status=result['status'], published_scopes=len(scopes), assumptions=len(affected))))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', type=Path, required=True)
    parser.add_argument('--original-study', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    publish(args.study, args.original_study, args.output)
