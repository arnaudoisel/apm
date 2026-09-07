"""Collection regression tests for live MCP registry integration coverage."""

from __future__ import annotations

import runpy
import socket
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.component


def test_mcp_registry_e2e_collection_import_does_not_open_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The collection import phase for the live E2E module must not probe the registry."""
    repo_root = Path(__file__).resolve().parents[2]
    attempts_log = tmp_path / "network-attempts.log"
    home = tmp_path / "home"
    home.mkdir()

    def record(kind: str, target: object) -> None:
        with attempts_log.open("a", encoding="utf-8") as handle:
            handle.write(f"{kind}: {target!r}\n")

    def blocked_connect(self, address):
        record("connect", address)
        raise RuntimeError("network attempted during collection")

    def blocked_create_connection(address, *args, **kwargs):
        record("create_connection", address)
        raise RuntimeError("network attempted during collection")

    def blocked_getaddrinfo(host, *args, **kwargs):
        record("getaddrinfo", host)
        raise RuntimeError("network attempted during collection")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)
    monkeypatch.setattr(socket, "create_connection", blocked_create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", blocked_getaddrinfo)
    monkeypatch.setenv("APM_NO_CACHE", "1")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MCP_REGISTRY_CONNECT_TIMEOUT", "0.001")
    monkeypatch.setenv("MCP_REGISTRY_READ_TIMEOUT", "0.001")
    monkeypatch.delenv("MCP_REGISTRY_URL", raising=False)
    monkeypatch.syspath_prepend(str(repo_root / "src"))
    sys.modules.pop("test_mcp_registry_e2e", None)
    sys.modules.pop("tests.integration.test_mcp_registry_e2e", None)

    runpy.run_path(
        str(repo_root / "tests" / "integration" / "test_mcp_registry_e2e.py"),
        run_name="__apm_collection_probe__",
    )

    assert not attempts_log.exists(), attempts_log.read_text(encoding="utf-8")
