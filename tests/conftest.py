"""Explicit software orchestration doubles, never financial qualification.

Only marked legacy CLI orchestration suites replace the new external financial
boundary. Their numerical, provenance, temporal and failure assertions remain
unchanged. Admission tests are unmarked and always execute the real gate.
"""

import socket

import pytest


@pytest.fixture(autouse=True, scope="module")
def synthetic_cli_admission(request):
    if not any(
        marker.name == "usefixtures" and "synthetic_cli_admission" in marker.args
        for marker in request.node.iter_markers()
    ):
        yield
        return
    from factorlab import cli

    def offline(*args, **kwargs):
        raise AssertionError("synthetic orchestration must remain offline")

    def software_boundary(*args, **kwargs):
        return {
            "schema": "SOFTWARE_ORCHESTRATION_TEST_DOUBLE_V1",
            "status": "SOFTWARE_ONLY", "formal_admission": False,
            "execution_admission": False, "financial_data_qualified": False,
            "no_live_money": True,
        }

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket.socket, "connect", offline)
        patch.setattr(socket, "create_connection", offline)
        patch.setattr(cli, "assert_formal_market_admission", software_boundary)
        patch.setattr(cli, "_admit_forward_evaluation", software_boundary)
        yield
