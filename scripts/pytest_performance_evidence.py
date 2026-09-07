"""Preserve original pytest node IDs before xdist appends scheduling groups."""

from __future__ import annotations

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Record a stable identity without changing selection, grouping, or outcomes."""
    for item in items:
        item.user_properties.append(("apm_performance_nodeid", item.nodeid))
