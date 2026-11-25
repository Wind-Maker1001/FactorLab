"""Type-preserving content identities independent of mapping insertion order."""
from hashlib import sha256

from factorlab.trading.storage import dumps


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonical(item) for item in value]
    return value


def content_identity(value):
    return sha256(dumps(canonical(value)).encode()).hexdigest()
