"""S02 selection-result boundary; research promotion alone grants no Demo capability."""
from .strategy_package import digest


SCHEMA = 'FACTORLAB_CHAMPION_SELECTION_V1'
EXECUTION = 'binance_usdm_demo_nvda_limit_long_only'


def adapt_selection(selection, manifest):
    """Consume native S02 results, keeping incompatible/no-promotion results inert.

    S02 currently emits A-share research; it must supply a matching Demo research
    execution model before such a winner can be activated on this account.
    """
    if selection.get('schema') != SCHEMA:
        raise ValueError('unsupported Champion Selector contract')
    if selection.get('promoted') is not True:
        return None
    common = selection.get('common', {})
    if (common.get('symbol') != manifest['symbol'] or common.get('execution') != EXECUTION
            or selection.get('source_kind') != 'REAL_CANDIDATE'):
        raise ValueError('research evidence incompatible with Demo execution')
    # Preserve the research tag. This adapter grants no live-money or extra account authority.
    if manifest['evidence'].get('financial_admission') != selection.get('financial_admission'):
        raise ValueError('financial evidence classification was changed')
    selected = selection['selected']
    candidate_id = selected['candidate_id']
    decision = next((d for d in selection['decisions'] if d['candidate_id'] == candidate_id), None)
    evidence = next((c for c in selection['candidates'] if c['candidate']['candidate_id'] == candidate_id), None)
    if (not decision or decision.get('eligible') is not True or decision.get('reasons')
            or not evidence or evidence.get('status') != 'OOS_QUALIFIED'
            or evidence['candidate'] != selected):
        raise ValueError('selected candidate lacks qualified OOS decision')
    test = evidence['stages']['test']
    source_path = selected['strategy']['file'].replace('\\', '/').lstrip('./')
    source_name = source_path.rsplit('/', 1)[-1]
    file_hash = manifest['files'].get(source_path) or manifest['files'].get(source_name)
    required = dict(selection_sha256=digest(selection), candidate_id=candidate_id,
                    run_id=test['run_id'], record_sha256=test['record_sha256'])
    if (test.get('status') != 'SUCCEEDED' or manifest['configuration'] != selected['parameters']
            or file_hash != selected['code_version']
            or any(manifest['evidence'].get(k) != v for k, v in required.items())):
        raise ValueError('selector/package code, configuration or evidence binding mismatch')
    return dict(schema_version=1, decision='PROMOTE', qualified=True, strictly_better=True,
        selection_id=f"{selection['study_id']}:{selection['version']}:{digest(selection)}",
        expected_active=manifest['evidence']['expected_active'],
        package_id=manifest['package_id'], evidence=manifest['evidence'])
