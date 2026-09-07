"""Reduced CI provisioning must not silently skip runtime regression contracts."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from tests.integration import conftest

pytestmark = pytest.mark.component


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
