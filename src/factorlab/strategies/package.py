"""Content-addressed strategy releases; market authority is supplied by adapters."""
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import zipfile
from decimal import Decimal

from .protocol import StrategySpecV2, V1, V2

INTERFACE = 'factorlab.demo.evaluate/v1'
DECISION_INTERFACE = V1
SUPPORTED_INTERFACES = frozenset({INTERFACE, V1, V2})


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return sha256(encoded(value)).hexdigest()


def package_entrypoint_file(manifest):
    module, symbol = manifest['entrypoint'].split(':')
    return (module if module.endswith('.py') else module.replace('.', '/') + '.py'), symbol


def manifest_spec(manifest):
    requirements = manifest['requirements']
    capital = dict(requirements['capital'])
    if not isinstance(capital['requested'], str):
        raise ValueError('manifest capital must use a decimal string')
    capital['requested'] = Decimal(capital['requested'])
    return StrategySpecV2(strategy_id=manifest['strategy_id'], version=manifest['strategy_version'],
        market=manifest['market'], account_id=manifest['account_id'], symbols=manifest['symbols'],
        decision_kind=manifest['execution_mode'], data=requirements['data'], schedule=requirements['schedule'],
        state=requirements['state'], capital=capital, execution=requirements['execution'])


def strategy_package_identity(manifest):
    interface = manifest['interface']
    if interface == V2:
        return dict(strategy_id=manifest['strategy_id'], strategy_version=manifest['strategy_version'],
            package_id=manifest['package_id'], code_commit=manifest['code_commit'],
            files_sha256=digest(manifest['files']), configuration_sha256=digest(manifest['configuration']),
            protocol_version=interface, state_schema_version=manifest['requirements']['state']['version'],
            execution_mode=manifest['execution_mode'], market=manifest['market'], account_id=manifest['account_id'],
            symbols=manifest['symbols'], research_evidence_sha256=digest(manifest['evidence']))
    mode = manifest.get('execution_mode')
    if mode is None and interface == INTERFACE:
        mode = 'evaluate_v1'
    return dict(strategy_id=manifest.get('strategy_id') or manifest['entrypoint'],
        strategy_version=str(manifest.get('strategy_version', manifest['state_version'])),
        package_id=manifest['package_id'], code_commit=manifest['code_commit'],
        files_sha256=digest(manifest['files']), configuration_sha256=digest(manifest['configuration']),
        protocol_version=interface, state_schema_version=str(manifest['state_version']), execution_mode=mode,
        instrument=manifest['symbol'], interval_seconds=manifest.get('interval_seconds', 60),
        research_evidence_sha256=digest(manifest['evidence']))


def validate_manifest(m, *, qualify=None):
    interface = m.get('interface')
    if ((m.get('schema_version'), interface) not in {(1, INTERFACE), (1, V1), (2, V2)}):
        raise ValueError('unsupported StrategyPackage/interface version')
    if interface != V2:
        if m.get('environment') != 'BINANCE_USDM_DEMO' or m.get('symbol') != 'NVDAUSDT':
            raise ValueError('package outside existing Demo authority')
        if interface == V1 and m.get('interval_seconds', 60) not in {
                60, 180, 300, 900, 1800, 3600, 7200, 14400, 21600, 28800, 43200, 86400}:
            raise ValueError('interval_seconds unavailable from legacy Demo contract')
    if not re.fullmatch(r'[0-9a-f]{40}', m['code_commit']):
        raise ValueError('full source commit required')
    if interface == V2:
        if 'symbol' in m or 'environment' in m:
            raise ValueError('V2 uses market/account/symbols, not legacy authority fields')
        manifest_spec(m)
    elif interface == INTERFACE:
        if type(m['state_version']) is not int or m['state_version'] < 1:
            raise ValueError('positive state version required')
    else:
        state_version = m.get('state_version')
        if not ((type(state_version) is int and state_version >= 1)
                or isinstance(state_version, str) and state_version):
            raise ValueError('positive state version or nonempty schema version required')
        if not isinstance(m.get('strategy_id'), str) or not m['strategy_id']:
            raise ValueError('strategy_id required for FACTORLAB_DECISION_V1 package')
        if not isinstance(m.get('strategy_version'), str) or not m['strategy_version']:
            raise ValueError('strategy_version required for FACTORLAB_DECISION_V1 package')
        if m.get('execution_mode') is not None and m['execution_mode'] not in {'target', 'orders'}:
            raise ValueError('execution_mode must be target or orders')
        interval = m.get('interval_seconds', 60)
        if type(interval) is not int or interval <= 0:
            raise ValueError('positive interval_seconds required')
    if 'package' in m and not isinstance(m['package'], bool):
        raise ValueError('package flag must be a boolean')
    if m['migration'] not in {'wait_flat', 'reduce_only', 'artifact_refresh'}:
        raise ValueError('explicit position migration policy required')
    if m['migration'] == 'reduce_only' and m.get('reduction', {}).get('explicitly_allowed') is not True:
        raise ValueError('reduction requires an explicit preconfigured rule')
    if m.get('reduction') and (type(m['reduction'].get('max_attempts')) is not int
                             or not 1 <= m['reduction']['max_attempts'] <= 10):
        raise ValueError('bounded reduction attempts required')
    if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*', m['entrypoint']):
        raise ValueError('module:function entrypoint required')
    if not isinstance(m['configuration'], dict) or not isinstance(m['evidence'], dict) or not m['evidence']:
        raise ValueError('configuration and research evidence required')
    for requirement in m['dependencies']:
        if not re.fullmatch(r'[A-Za-z0-9_.-]+==[A-Za-z0-9_.+!-]+', requirement):
            raise ValueError('dependencies must be pinned; no install hooks/URLs')
    files = m['files']
    if not files or len(files) > 256:
        raise ValueError('bounded source file inventory required')
    for name, fingerprint in files.items():
        p = PurePosixPath(name)
        if (p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name
                or str(p) != name or not name.endswith('.py') or len(p.parts) > 10
                or not re.fullmatch(r'[0-9a-f]{64}', fingerprint)):
            raise ValueError('invalid package source member')
    if package_entrypoint_file(m)[0] not in files:
        raise ValueError('entrypoint missing from source inventory')
    if 'model_artifact' in m:
        if interface != V2:
            raise ValueError('model_artifact metadata requires V2 StrategyPackage')
        ma = m['model_artifact']
        required_ma = {'kind', 'schema_version', 'file', 'artifact_sha256', 'covered_start', 'covered_end'}
        if not isinstance(ma, dict) or not required_ma <= set(ma):
            raise ValueError('invalid model_artifact metadata')
        if (not isinstance(ma['kind'], str) or not ma['kind']
                or type(ma['schema_version']) is not int or ma['schema_version'] < 1
                or not isinstance(ma['file'], str) or ma['file'] not in files
                or ma['file'] == package_entrypoint_file(m)[0]
                or not isinstance(ma['artifact_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', ma['artifact_sha256'])
                or not isinstance(ma['covered_start'], str)
                or not isinstance(ma['covered_end'], str)
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', ma['covered_start'])
                or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', ma['covered_end'])
                or ma['covered_start'] > ma['covered_end']):
            raise ValueError('invalid model_artifact metadata fields')
        if 'model_artifact_sha256' in m['evidence'] and m['evidence']['model_artifact_sha256'] != ma['artifact_sha256']:
            raise ValueError('evidence model_artifact_sha256 differs from model_artifact metadata')
    if m['migration'] == 'artifact_refresh':
        if (interface != V2 or 'model_artifact' not in m
                or m['requirements']['state'].get('migration') != 'preserve'):
            raise ValueError('artifact_refresh requires V2 package with model_artifact metadata and preserve state migration')
    if m.get('package_id') != digest({k: v for k, v in m.items() if k != 'package_id'}):
        raise ValueError('package identity mismatch')
    if qualify is not None:
        qualify(m)
    return m


def build_package(source, manifest, destination, *, qualify=None):
    """Only inventoried Python files are copied; dependencies are never installed."""
    source, destination = Path(source), Path(destination)
    m = dict(manifest)
    for name in m['files']:
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name
                or str(path) != name or not name.endswith('.py')
                or source.resolve() not in (source / name).resolve().parents
                or (source / name).is_symlink()):
            raise ValueError('invalid source path')
    m['files'] = {name: sha256((source / name).read_bytes()).hexdigest() for name in m['files']}
    m['package_id'] = digest({k: v for k, v in m.items() if k != 'package_id'})
    validate_manifest(m, qualify=qualify)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in [('manifest.json', encoded(m))] + [
                (n, (source / n).read_bytes()) for n in sorted(m['files'])]:
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.external_attr = 0o100444 << 16
            z.writestr(info, data)
    return m


def install_package(archive, releases, *, qualify=None):
    releases = Path(releases)
    releases.mkdir(parents=True, exist_ok=True, mode=0o700)
    with zipfile.ZipFile(archive) as z:
        if len(z.infolist()) > 257 or sum(i.file_size for i in z.infolist()) > 4 * 1024 * 1024:
            raise ValueError('oversized package')
        m = validate_manifest(json.loads(z.read('manifest.json')), qualify=qualify)
        if sorted(z.namelist()) != sorted(['manifest.json', *m['files']]):
            raise ValueError('unlisted or duplicate package members')
        payload = {name: z.read(name) for name in m['files']}
        if any(sha256(data).hexdigest() != m['files'][name] for name, data in payload.items()):
            raise ValueError('source identity mismatch')
    target = releases / m['package_id']
    if target.exists():
        if load_package(target, qualify=qualify) != m:
            raise ValueError('immutable release collision')
        return target, m
    temp = Path(tempfile.mkdtemp(prefix='.staging-', dir=releases))
    try:
        for name, data in payload.items():
            path = temp / name
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(data)
            path.chmod(0o600)
        (temp / 'manifest.json').write_bytes(encoded(m))
        (temp / 'manifest.json').chmod(0o600)
        temp.rename(target)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return target, m


def load_package(path, *, qualify=None):
    path = Path(path)
    m = validate_manifest(json.loads((path / 'manifest.json').read_bytes()), qualify=qualify)
    for name, fingerprint in m['files'].items():
        member = path / name
        if member.is_symlink() or not member.resolve().is_relative_to(path.resolve()):
            raise ValueError('linked package source')
        if sha256(member.read_bytes()).hexdigest() != fingerprint:
            raise ValueError('installed source identity mismatch')
    return m


def load_package_sources(path, *, qualify=None):
    """Verify installed release hashes and read sources without importing or executing any code."""
    root = Path(path).resolve()
    m = load_package(root, qualify=qualify)
    sources = {name: (root / name).read_text(encoding='utf-8') for name in sorted(m['files'])}
    return m, sources


def verify_packaged_model_artifact(manifest, sources):
    """Pure-AST, zero-execution verification of a package's declared model_artifact source file."""
    validate_manifest(manifest)
    if 'model_artifact' not in manifest:
        raise ValueError('package has no declared model_artifact metadata')
    ma = manifest['model_artifact']
    artifact_file = ma['file']
    if artifact_file not in sources:
        raise ValueError('missing declared model_artifact source file')
    raw_text = sources[artifact_file]
    expected_hash = manifest['files'][artifact_file]
    lf_hash = sha256(raw_text.encode('utf-8')).hexdigest()
    crlf_hash = sha256(raw_text.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8')).hexdigest()
    if expected_hash not in {lf_hash, crlf_hash}:
        raise ValueError('model_artifact source digest mismatch')
    kind = ma['kind']
    if kind == 'qlib_v100_monthly_ridge':
        from .qlib_blend_ridge import extract_frozen_artifact_from_module_source
        art = extract_frozen_artifact_from_module_source(raw_text)
        if art['artifact_sha256'] != ma['artifact_sha256']:
            raise ValueError('model_artifact artifact_sha256 mismatch')
        if art['covered_start'] != ma['covered_start'] or art['covered_end'] != ma['covered_end']:
            raise ValueError('model_artifact coverage metadata mismatch')
        if 'omit_sndk' in ma and bool(art['omit_sndk']) != bool(ma['omit_sndk']):
            raise ValueError('model_artifact omit_sndk mismatch')
        if 'fitted_months' in ma and len(art['models']) != int(ma['fitted_months']):
            raise ValueError('model_artifact fitted_months mismatch')
        return art
    raise ValueError(f'unsupported model_artifact kind: {kind!r}')


def verify_artifact_refresh_manifests(old_manifest, new_manifest, *, direction='upgrade'):
    """Verify two V2 manifests differ ONLY in their declared frozen model artifact and metadata."""
    if direction not in {'upgrade', 'rollback'}:
        raise ValueError(f'unsupported artifact refresh direction: {direction!r}')
    validate_manifest(old_manifest)
    validate_manifest(new_manifest)
    if old_manifest['interface'] != V2 or new_manifest['interface'] != V2:
        raise ValueError('artifact_refresh requires V2 StrategyPackages')
    if 'model_artifact' not in old_manifest or 'model_artifact' not in new_manifest:
        raise ValueError('artifact_refresh requires model_artifact metadata on both old and new packages')
    for field in (
        'strategy_id',
        'strategy_version',
        'market',
        'account_id',
        'symbols',
        'execution_mode',
        'requirements',
        'entrypoint',
        'dependencies',
        'configuration',
    ):
        if old_manifest[field] != new_manifest[field]:
            raise ValueError(f'incompatible artifact_refresh: {field} changed')
    if bool(old_manifest.get('package', False)) != bool(new_manifest.get('package', False)):
        raise ValueError('incompatible artifact_refresh: package layout changed')
    if old_manifest['requirements']['state'].get('migration') != 'preserve':
        raise ValueError('incompatible artifact_refresh: state migration must be preserve')

    old_ma, new_ma = old_manifest['model_artifact'], new_manifest['model_artifact']
    for field in ('kind', 'schema_version', 'file'):
        if old_ma[field] != new_ma[field]:
            raise ValueError(f'incompatible artifact_refresh: model_artifact.{field} changed')
    if old_ma.get('omit_sndk') != new_ma.get('omit_sndk'):
        raise ValueError('incompatible artifact_refresh: model_artifact.omit_sndk changed')
    artifact_file = old_ma['file']
    old_code_files = {k: v for k, v in old_manifest['files'].items() if k != artifact_file}
    new_code_files = {k: v for k, v in new_manifest['files'].items() if k != artifact_file}
    if old_code_files != new_code_files:
        raise ValueError('incompatible artifact_refresh: non-artifact strategy code files changed')
    if direction == 'upgrade' and new_ma['covered_end'] < old_ma['covered_end']:
        raise ValueError('incompatible artifact_refresh: covered_end cannot move backwards on upgrade')


def can_artifact_refresh_manifests(old_manifest, new_manifest, *, direction='upgrade') -> bool:
    try:
        verify_artifact_refresh_manifests(old_manifest, new_manifest, direction=direction)
    except ValueError:
        return False
    return True


def verify_artifact_refresh_packages(
    old_manifest,
    new_manifest,
    *,
    old_sources,
    new_sources,
    direction='upgrade',
):
    """Full manifest + pure-AST frozen artifact payload compatibility check (zero code execution)."""
    verify_artifact_refresh_manifests(old_manifest, new_manifest, direction=direction)
    old_art = verify_packaged_model_artifact(old_manifest, old_sources)
    new_art = verify_packaged_model_artifact(new_manifest, new_sources)
    kind = old_manifest['model_artifact']['kind']
    if kind == 'qlib_v100_monthly_ridge':
        from .qlib_blend_ridge import verify_ridge_artifact_compatibility
        verify_ridge_artifact_compatibility(old_art, new_art, direction=direction)
    else:
        raise ValueError(f'unsupported model_artifact kind for artifact_refresh: {kind!r}')


def model_refresh_candidate(binding, manifest, *, expected_active, selection_id):
    """Build an evidence-bound FACTORLAB_MODEL_REFRESH_V2 request (not a Champion promotion)."""
    validate_manifest(manifest)
    if manifest['interface'] != V2 or manifest['migration'] != 'artifact_refresh' or 'model_artifact' not in manifest:
        raise ValueError('model_refresh_candidate requires a V2 artifact_refresh StrategyPackage')
    return dict(
        schema='FACTORLAB_MODEL_REFRESH_V2',
        binding=dict(binding),
        selection_id=str(selection_id),
        decision='REFRESH_MODEL',
        qualified=True,
        expected_active=expected_active,
        package_id=manifest['package_id'],
        model_artifact_sha256=manifest['model_artifact']['artifact_sha256'],
        evidence=manifest['evidence'],
    )


def candidate_package(candidate, manifest):
    """Bind promotion or compatible model-refresh evidence, without interpreting research metrics."""
    v2 = manifest['interface'] == V2
    if v2 and candidate.get('schema') == 'FACTORLAB_MODEL_REFRESH_V2':
        if (manifest['migration'] != 'artifact_refresh' or 'model_artifact' not in manifest
                or candidate.get('decision') != 'REFRESH_MODEL' or candidate.get('qualified') is not True
                or candidate.get('package_id') != manifest['package_id']
                or candidate.get('model_artifact_sha256') != manifest['model_artifact']['artifact_sha256']
                or candidate.get('evidence') != manifest['evidence'] or not candidate.get('selection_id')):
            raise ValueError('qualified, artifact-bound model refresh request required')
        manifest_spec(manifest).require_binding(candidate['binding'])
        return str(candidate['selection_id'])
    if ((candidate.get('schema') != 'FACTORLAB_CHAMPION_PROMOTION_V2' if v2 else candidate.get('schema_version') != 1)
            or candidate.get('decision') != 'PROMOTE' or candidate.get('qualified') is not True
            or candidate.get('strictly_better') is not True or candidate.get('package_id') != manifest['package_id']
            or candidate.get('evidence') != manifest['evidence'] or not candidate.get('selection_id')):
        raise ValueError('qualified, better, evidence-bound champion required')
    if v2:
        manifest_spec(manifest).require_binding(candidate['binding'])
    return str(candidate['selection_id'])
