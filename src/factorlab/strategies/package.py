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
    if m['migration'] not in {'wait_flat', 'reduce_only'}:
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


def candidate_package(candidate, manifest):
    """Bind promotion evidence, without interpreting research metrics."""
    v2 = manifest['interface'] == V2
    if ((candidate.get('schema') != 'FACTORLAB_CHAMPION_PROMOTION_V2' if v2 else candidate.get('schema_version') != 1)
            or candidate.get('decision') != 'PROMOTE' or candidate.get('qualified') is not True
            or candidate.get('strictly_better') is not True or candidate.get('package_id') != manifest['package_id']
            or candidate.get('evidence') != manifest['evidence'] or not candidate.get('selection_id')):
        raise ValueError('qualified, better, evidence-bound champion required')
    if v2:
        manifest_spec(manifest).require_binding(candidate['binding'])
    return str(candidate['selection_id'])
