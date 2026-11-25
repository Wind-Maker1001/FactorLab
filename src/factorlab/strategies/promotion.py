"""Route adapter-qualified promotions by immutable market/account/instance binding."""
from contextlib import closing

from .package import candidate_package, validate_manifest
from .protocol import V2

SCHEMA = 'FACTORLAB_CHAMPION_PROMOTION_V2'


def route_promotion(store, promotion, manifest, *, mode='hot', boot='unknown'):
    """Request one instance switch; native research qualification stays in adapters."""
    validate_manifest(manifest, qualify=store.qualify)
    if manifest['interface'] != V2 or promotion.get('schema') != SCHEMA:
        raise ValueError('V2 promotion and package required')
    candidate_package(promotion, manifest)
    instance_id = promotion['binding']['instance_id']
    with closing(store.for_instance(instance_id)) as view:
        return view.request(manifest, promotion, mode=mode, boot=boot)
