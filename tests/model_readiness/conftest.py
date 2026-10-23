"""Independent offline suite; not part of the frozen tests/research collection."""

import socket

import pytest


@pytest.fixture(autouse=True, scope="session")
def prohibit_network():
    def rejected(*args, **kwargs):
        raise AssertionError("model readiness acceptance must remain offline")
    # Session scope also protects module-scoped producer fixtures.
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(socket.socket, "connect", rejected)
        monkeypatch.setattr(socket, "create_connection", rejected)
        yield
