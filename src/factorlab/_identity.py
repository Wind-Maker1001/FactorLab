"""Candidate software identity, never a financial authorization mechanism."""
import hashlib
import importlib.metadata
from pathlib import Path
import subprocess

BASELINE_SHA = '0ec7af5870c54a0ee202fbceeb97b7d87c293411'
MIGRATION_PROTOCOL = 'FACTORLAB_NAMESPACE_MIGRATION_V1'


def package_root():
    return Path(__file__).resolve().parent


def source_context_root():
    package = package_root()
    candidate = package.parent.parent
    if package == candidate/'src/factorlab' and (candidate/'pyproject.toml').is_file():
        return candidate
    return package.parent


def software_identity():
    package, root = package_root(), source_context_root()
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(package.rglob('*.py')) if '__pycache__' not in p.parts}
    commit = None
    if root/'src/factorlab' == package and (root/'.git').exists():
        try:
            commit = subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],
                stderr=subprocess.DEVNULL,text=True).strip()
        except (OSError,subprocess.CalledProcessError):
            pass
    try:
        version = importlib.metadata.version('factorlab')
    except importlib.metadata.PackageNotFoundError:
        version = 'UNINSTALLED_SOURCE'
    return {'protocol':MIGRATION_PROTOCOL,'software_name':'FactorLab','package':'factorlab',
            'version':version,'migration_baseline':BASELINE_SHA,'git_commit':commit,
            'source_sha256':files,'identity_is_authorization':False,
            'formal_admission':'FORMAL_ADMISSION_BLOCKED',
            'trust_root_review':'BLOCKED_INDEPENDENT_REVIEW_REQUIRED'}


def assert_migration_admitted():
    from factorlab.errors import ProviderError
    raise ProviderError('MARKET DATA QUALIFICATION REQUIRED: FORMAL_ADMISSION_BLOCKED; '
                        'MIGRATION_TRUST_ROOT_UNREVIEWED; independent authorization required')
