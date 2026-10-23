"""Independent security version selection; no local authority to issue approval."""

CONTROL_VERSION = "FACTORLAB_INDEPENDENT_SECURITY_V1"
REQUIRED_STAGES = frozenset({"RAW", "ADJUSTMENT", "STATUS", "PIT", "SOURCE_BINDING", "FORMAL"})


def assert_independent_admission():
    from factorlab.errors import ProviderError
    raise ProviderError(
        "MARKET DATA QUALIFICATION REQUIRED: FORMAL_ADMISSION_BLOCKED; "
        "MIGRATION_TRUST_ROOT_UNREVIEWED; " + CONTROL_VERSION +
        "; independent authorization required; V1_HAS_NO_INDEPENDENT_APPROVAL_MECHANISM"
    )
