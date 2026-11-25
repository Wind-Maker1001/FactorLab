"""Binance package qualification over the market-independent release store."""
from factorlab.strategies import package as _packages
from factorlab.strategies.package import (
    INTERFACE, DECISION_INTERFACE, SUPPORTED_INTERFACES, encoded, digest,
    package_entrypoint_file, strategy_package_identity, candidate_package,
)
from factorlab.strategies.protocol import V2
from .demo_market_feed import INTERVALS


def qualify_manifest(manifest):
    if manifest['interface'] == V2:
        if manifest['market'] != 'BINANCE_USDM':
            raise ValueError('package outside Binance USD-M authority')
        # Dynamic-symbol admission requires the caller's account-bound registry.
        raise ValueError('V2 requires an explicit account/symbol qualification callback')
    if manifest.get('environment') != 'BINANCE_USDM_DEMO' or manifest.get('symbol') != 'NVDAUSDT':
        raise ValueError('package outside existing Demo authority')
    if manifest['interface'] == DECISION_INTERFACE and manifest.get('interval_seconds', 60) not in INTERVALS.values():
        raise ValueError('interval_seconds unavailable from current Demo feed')


def _qualification(manifest, qualify):
    if manifest['interface'] == V2 and manifest['market'] != 'BINANCE_USDM':
        raise ValueError('package outside Binance USD-M authority')
    if manifest['interface'] != V2 or qualify is None:
        qualify_manifest(manifest)
    if qualify is not None:
        qualify(manifest)


def validate_manifest(manifest, *, qualify=None):
    return _packages.validate_manifest(manifest, qualify=lambda m: _qualification(m, qualify))


def build_package(source, manifest, destination, *, qualify=None):
    return _packages.build_package(source, manifest, destination, qualify=lambda m: _qualification(m, qualify))


def install_package(archive, releases, *, qualify=None):
    return _packages.install_package(archive, releases, qualify=lambda m: _qualification(m, qualify))


def load_package(path, *, qualify=None):
    return _packages.load_package(path, qualify=lambda m: _qualification(m, qualify))
