"""Independent offline suite; not part of the frozen tests/research collection."""

import socket

import pytest


@pytest.fixture(autouse=True, scope="module")
def prohibit_network():
    def rejected(*args, **kwargs):
        raise AssertionError("model readiness acceptance must remain offline")
    # Cover module-scoped producers without leaking the patch into other modules.
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(socket.socket, "connect", rejected)
        monkeypatch.setattr(socket, "create_connection", rejected)
        yield
