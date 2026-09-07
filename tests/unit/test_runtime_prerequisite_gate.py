"""Reduced CI provisioning must not silently skip runtime regression contracts."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests.integration import conftest

pytestmark = pytest.mark.component


@pytest.mark.parametrize("strict", [False, True])
def test_parent_directory_collection_registers_runtime_option_before_child_hooks(
    tmp_path: Path, strict: bool
) -> None:
    """Whole-tree collection must register options before loading integration hooks."""
    root = Path(__file__).resolve().parents[2]
    integration = tmp_path / "tests" / "integration"
    integration.mkdir(parents=True)
    shutil.copyfile(root / "tests/conftest.py", tmp_path / "tests/conftest.py")
    source = (root / "tests/integration/conftest.py").read_text(encoding="utf-8")
    (integration / "conftest.py").write_text(
        source + "\n\ndef _has_runtime(name):\n    return False\n",
        encoding="utf-8",
    )
    (integration / "test_runtime.py").write_text(
        "import pytest\n"
        "pytestmark = pytest.mark.requires_runtime_codex\n"
        "def test_runtime_case():\n"
        "    pass\n",
        encoding="utf-8",
    )
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers = requires_runtime_codex: requires codex\n",
        encoding="utf-8",
    )
    command = [sys.executable, "-m", "pytest", "--collect-only", "--strict-markers", "-q"]
    if strict:
        command.append("--strict-runtime-prerequisites")
    result = subprocess.run(
        [*command, "tests"],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(root), str(root / "src")))},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == (4 if strict else 0), output
    if strict:
        assert "required runtime missing" in output
    else:
        assert "test_runtime_case" in output


@pytest.mark.parametrize("runtime", ["copilot", "codex", "llm"])
@pytest.mark.parametrize("strict", [False, True])
def test_missing_selected_runtime_fails_closed_only_for_provisioned_ci(
    monkeypatch: pytest.MonkeyPatch, runtime: str, strict: bool
) -> None:
    config = Mock(spec=pytest.Config)
    config.getoption.return_value = strict
    item = Mock(spec=pytest.Item)
    item.nodeid = "tests/integration/test_example.py::test_requires_runtime"
    item.get_closest_marker.side_effect = lambda name: (
        Mock() if name == f"requires_runtime_{runtime}" else None
    )
    monkeypatch.setattr(conftest, "_has_runtime", lambda name: False)

    if strict:
        with pytest.raises(pytest.UsageError, match="required runtime missing"):
            conftest.pytest_collection_modifyitems(config, [item])
        item.add_marker.assert_not_called()
    else:
        conftest.pytest_collection_modifyitems(config, [item])
        assert item.add_marker.call_args.args[0].name == "skip"


def test_unselected_runtime_does_not_require_provisioning(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = Mock(return_value=False)
    monkeypatch.setattr(conftest, "_has_runtime", probe)
    config = Mock(spec=pytest.Config)
    config.getoption.return_value = True
    conftest.pytest_collection_modifyitems(config, [])
    probe.assert_not_called()


def test_selected_available_runtime_still_executes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(conftest, "_has_runtime", lambda name: True)
    config = Mock(spec=pytest.Config)
    config.getoption.return_value = True
    item = Mock(spec=pytest.Item)
    item.get_closest_marker.side_effect = lambda name: (
        Mock() if name == "requires_runtime_copilot" else None
    )
    conftest.pytest_collection_modifyitems(config, [item])
    item.add_marker.assert_not_called()
