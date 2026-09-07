"""MCP config-only E2E coverage must not require runtime binaries."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration import test_mcp_env_var_copilot_e2e as mcp_env_e2e

pytestmark = pytest.mark.component

REPO_ROOT = Path(__file__).resolve().parents[2]
MCP_ENV_E2E = "tests/integration/test_mcp_env_var_copilot_e2e.py"
EXPECTED_CASES = {
    f"{MCP_ENV_E2E}::TestMcpEnvVarHeadersCopilot::"
    "test_self_defined_http_server_translates_env_vars_not_resolves",
    f"{MCP_ENV_E2E}::TestMcpEnvVarHeadersCopilot::"
    "test_self_defined_stdio_server_translates_env_vars_in_args",
    f"{MCP_ENV_E2E}::TestMcpEnvVarHeadersCursor::test_cursor_still_resolves_env_vars_to_literal",
}


def _module_pytest_marks() -> list[pytest.Mark]:
    """Return normalized module-level pytest marks for the config-only E2E module."""
    raw_marks = mcp_env_e2e.pytestmark
    if not isinstance(raw_marks, list):
        raw_marks = [raw_marks]
    return raw_marks


def test_config_only_mcp_e2e_keeps_binary_gate_without_copilot_runtime() -> None:
    """The E2E still needs candidate apm, but not the Copilot CLI binary."""
    marks = _module_pytest_marks()
    names = [mark.name for mark in marks]

    assert "requires_apm_binary" in names
    assert "requires_runtime_copilot" not in names
    assert any(
        mark.name == "xdist_group" and mark.kwargs.get("name") == "home_env" for mark in marks
    )


def test_config_only_mcp_e2e_collects_without_copilot_under_strict_prerequisites(
    tmp_path: Path,
) -> None:
    """Strict runtime prereqs must not deselect config-only Copilot rendering tests."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_apm = fake_bin / "apm"
    fake_apm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_apm.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    env = os.environ.copy()
    env["APM_BINARY_PATH"] = str(fake_apm)
    env["HOME"] = str(fake_home)
    env["PATH"] = str(fake_bin)
    git_executable = shutil.which("git")
    if git_executable is not None:
        env["GIT_PYTHON_GIT_EXECUTABLE"] = git_executable

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--collect-only",
            "--strict-runtime-prerequisites",
            "-q",
            MCP_ENV_E2E,
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    collected = {
        line.strip() for line in result.stdout.splitlines() if line.startswith(f"{MCP_ENV_E2E}::")
    }

    assert result.returncode == 0, output
    assert collected == EXPECTED_CASES
    assert "required runtime missing" not in output
    assert not (fake_home / ".apm" / "runtimes" / "copilot").exists()
