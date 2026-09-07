"""Preserve original pytest node IDs before xdist appends scheduling groups."""

from __future__ import annotations

import pytest

_PARENT_NODEID_PROPERTY = "apm_performance_parent_nodeid"
_ORIGINAL_NODEIDS: set[str] = set()
_SUBTEST_ORDINALS: dict[str, int] = {}
_IS_XDIST_WORKER = False


def pytest_configure(config: pytest.Config) -> None:
    """Keep nested/in-process pytest sessions from reusing stale identities."""
    global _IS_XDIST_WORKER
    _ORIGINAL_NODEIDS.clear()
    _SUBTEST_ORDINALS.clear()
    _IS_XDIST_WORKER = hasattr(config, "workerinput")


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Record a stable identity without changing selection, grouping, or outcomes."""
    for item in items:
        _ORIGINAL_NODEIDS.add(item.nodeid)
        item.user_properties.append(("apm_performance_nodeid", item.nodeid))


def _original_nodeid(nodeid: str) -> str:
    """Undo pytest-xdist's loadgroup suffix only when it matches a collected item."""
    if nodeid in _ORIGINAL_NODEIDS:
        return nodeid
    candidate, separator, _group = nodeid.rpartition("@")
    if separator and candidate in _ORIGINAL_NODEIDS:
        return candidate
    return nodeid


def _pop_worker_parent_nodeid(report: pytest.TestReport) -> str | None:
    """Read the worker-captured parent nodeid without leaking it to JUnit."""
    parents = [
        value
        for name, value in report.user_properties
        if name == _PARENT_NODEID_PROPERTY and isinstance(value, str) and value
    ]
    report.user_properties = [
        (name, value) for name, value in report.user_properties if name != _PARENT_NODEID_PROPERTY
    ]
    if len(parents) > 1:
        raise ValueError("Duplicate performance subtest parent identity")
    return parents[0] if parents else None


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Give unittest subTest reports their own JUnit node instead of a summary-only count.

    Pytest 9 emits a passed call report for each ``unittest.TestCase.subTest`` but
    its JUnit writer otherwise folds those passed subtests into the parent
    ``<testcase>`` while still incrementing ``tests``.  Performance evidence
    needs the strict XML summary count to equal the concrete scenario inventory,
    so split each subtest report into a stable synthetic child node before
    junitxml sees it.
    """
    if getattr(report, "when", None) != "call" or not hasattr(report, "context"):
        return
    if _IS_XDIST_WORKER:
        report.user_properties.append((_PARENT_NODEID_PROPERTY, _original_nodeid(report.nodeid)))
        return
    parent_nodeid = _pop_worker_parent_nodeid(report) or _original_nodeid(report.nodeid)
    _SUBTEST_ORDINALS[parent_nodeid] = _SUBTEST_ORDINALS.get(parent_nodeid, 0) + 1
    report.nodeid = f"{parent_nodeid}::subTest[{_SUBTEST_ORDINALS[parent_nodeid]}]"
    report.user_properties.append(("apm_performance_nodeid", report.nodeid))
