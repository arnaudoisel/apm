"""Performance claims require identical, complete, successful test evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.compare_test_runs import Report, capture, compare, read_report

pytestmark = pytest.mark.component
SHA = "a" * 40
IDENTITY = {
    "source_sha": SHA,
    "selection": "not live",
    "candidate": None,
    "environment": {
        "python": "3.12.12",
        "system": "Darwin",
        "release": "24.6.0",
        "machine": "arm64",
        "cpu_count": "3",
        "runner_image": "20260901",
        "dependency_lock": "e" * 64,
    },
}
CASES = {("suite", "test_a"): "passed", ("suite", "test_b"): "skipped"}


def _baseline() -> Report:
    return Report(IDENTITY, "baseline", 1, 1, 10.0, CASES)


def _proposed() -> list[Report]:
    return [
        Report(IDENTITY, "proposed", 1, 2, 5.0, {("suite", "test_a"): "passed"}),
        Report(IDENTITY, "proposed", 2, 2, 6.0, {("suite", "test_b"): "skipped"}),
    ]


def test_complete_disjoint_shards_preserve_skips_and_report_wall_separately_from_cost() -> None:
    result = compare([_baseline()], _proposed(), SHA, 0.05)
    assert result["scenario_count"] == 2
    assert result["outcomes"] == {"passed": 1, "skipped": 1, "xfailed": 0, "failed": 0, "error": 0}
    assert result["baseline_pytest_critical_seconds"] == 10.0
    assert result["proposed_pytest_critical_seconds"] == 6.0
    assert result["proposed_pytest_runner_seconds"] == 11.0
    assert result["speedup_fraction"] == pytest.approx(0.4)
    assert result["meets_threshold"] is True


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "overlap", "identity", "selection", "sha", "environment"]
)
def test_rejects_missing_overlap_and_identity_drift(fault: str) -> None:
    proposed = _proposed()
    expected = SHA
    if fault == "missing":
        proposed.pop()
    elif fault == "duplicate":
        proposed[1] = proposed[0]
    elif fault == "overlap":
        proposed[1] = replace(proposed[1], cases=proposed[0].cases)
    elif fault == "identity":
        proposed[1] = replace(
            proposed[1], identity={**IDENTITY, "candidate": {"digest": "different"}}
        )
    elif fault == "selection":
        proposed[1] = replace(proposed[1], identity={**IDENTITY, "selection": "smoke"})
    elif fault == "environment":
        proposed[1] = replace(
            proposed[1],
            identity={
                **IDENTITY,
                "environment": {**IDENTITY["environment"], "runner_image": "new"},
            },
        )
    else:
        expected = "b" * 40
    with pytest.raises(ValueError):
        compare([_baseline()], proposed, expected, 0.05)


@pytest.mark.parametrize("outcome", ["skipped", "xfailed", "failed", "error"])
def test_cannot_gain_speed_by_skipping_or_failing_a_previously_passing_case(outcome: str) -> None:
    proposed = _proposed()
    proposed[0] = replace(proposed[0], cases={("suite", "test_a"): outcome})
    with pytest.raises(ValueError):
        compare([_baseline()], proposed, SHA, 0.05)


@pytest.mark.parametrize("cases", [{("suite", "test_extra"): "passed"}, {}])
def test_rejects_extra_or_dropped_scenarios(cases: dict[tuple[str, str], str]) -> None:
    proposed = _proposed()
    proposed[0] = replace(proposed[0], cases=cases)
    with pytest.raises(ValueError):
        compare([_baseline()], proposed, SHA, 0.05)


def test_missed_speed_target_remains_explicit_in_durable_result() -> None:
    proposed = [replace(_baseline(), variant="proposed", elapsed_seconds=10.1)]
    result = compare([_baseline()], proposed, SHA, 0.05)
    assert result["same_scenarios_and_outcomes"] is True
    assert result["meets_threshold"] is False
    assert result["speedup_fraction"] < 0


@pytest.mark.parametrize("seconds", [float("nan"), float("inf"), 0, -1, True])
def test_rejects_unusable_timings(tmp_path: Path, seconds: float) -> None:
    payload = _baseline().payload()
    payload["elapsed_seconds"] = seconds
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(payload), encoding="ascii")
    with pytest.raises(ValueError, match="finite and positive"):
        read_report(path)


@pytest.mark.parametrize(
    "fault", ["duplicate", "unknown-outcome", "empty", "missing-field", "bool-shard"]
)
def test_rejects_malformed_inventory(tmp_path: Path, fault: str) -> None:
    payload = _baseline().payload()
    if fault == "duplicate":
        payload["cases"] = [{"id": ["suite", "a"], "outcome": "passed"}] * 2
    elif fault == "unknown-outcome":
        payload["cases"] = [{"id": ["suite", "a"], "outcome": "unknown"}]
    elif fault == "empty":
        payload["cases"] = []
    elif fault == "missing-field":
        del payload["identity"]
    else:
        payload["shard"] = True
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(payload), encoding="ascii")
    with pytest.raises(ValueError):
        read_report(path)


def test_actual_junit_capture_roundtrip_keeps_xfail_identity(tmp_path: Path) -> None:
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuites><testsuite tests="2" failures="0" errors="0" skipped="1" time="7.5">'
        '<testcase classname="suite" name="test_a" time="2.0"/>'
        '<testcase classname="suite" name="test_b" time="3.0">'
        '<skipped type="pytest.xfail" message="known failure"/></testcase>'
        "</testsuite></testsuites>",
        encoding="ascii",
    )
    report = capture(junit, SHA, "not live", "baseline", 1, 1)
    assert report.elapsed_seconds == 7.5
    assert report.cases == {("suite", "test_a"): "passed", ("suite", "test_b"): "xfailed"}
    path = tmp_path / "proof.json"
    path.write_text(json.dumps(report.payload()), encoding="ascii")
    assert read_report(path) == report


@pytest.mark.parametrize("fault", ["none", "source", "digest", "binary", "version", "schema"])
def test_candidate_evidence_is_bound_to_the_source_and_archive(tmp_path: Path, fault: str) -> None:
    metadata = {
        "schema_version": 1,
        "sha": SHA,
        "version": "0.30.0",
        "binary_name": "apm-darwin-arm64",
        "archive": "apm-darwin-arm64.tar.gz",
        "archive_sha256": "c" * 64,
        "executable_sha256": "d" * 64,
    }
    if fault == "source":
        metadata["sha"] = "b" * 40
    elif fault == "digest":
        metadata["archive_sha256"] = "not-a-digest"
    elif fault == "binary":
        metadata["binary_name"] = "apm-windows-x86_64"
    elif fault == "version":
        metadata["version"] = ""
    elif fault == "schema":
        metadata["schema_version"] = True
    path = tmp_path / "candidate.json"
    path.write_text(json.dumps(metadata), encoding="ascii")
    junit = tmp_path / "junit.xml"
    junit.write_text(
        '<testsuite tests="1" failures="0" errors="0" skipped="0" time="1">'
        '<testcase classname="suite" name="a"/></testsuite>',
        encoding="ascii",
    )
    if fault != "none":
        with pytest.raises(ValueError):
            capture(junit, SHA, "not live", "baseline", 1, 1, path)
    else:
        report = capture(junit, SHA, "not live", "baseline", 1, 1, path)
        assert report.identity["candidate"] == metadata


@pytest.mark.parametrize(
    "xml",
    [
        '<testsuite tests="0" failures="0" errors="0" skipped="0" time="1"/>',
        '<testsuite tests="2" failures="0" errors="0" skipped="0" time="1">'
        '<testcase classname="suite" name="a"/></testsuite>',
        '<testsuite tests="2" failures="0" errors="0" skipped="0" time="1">'
        '<testcase classname="suite" name="a"/><testcase classname="suite" name="a"/></testsuite>',
        "<testsuites><testsuite/><testsuite/></testsuites>",
    ],
)
def test_rejects_incomplete_or_ambiguous_junit(tmp_path: Path, xml: str) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(xml, encoding="ascii")
    with pytest.raises(ValueError):
        capture(path, SHA, "not live", "baseline", 1, 1)


@pytest.mark.parametrize(
    "declaration",
    [
        '<!DOCTYPE testsuite [<!ENTITY injected "expanded">]>',
        '<!DOCTYPE testsuite SYSTEM "file:///etc/passwd">',
    ],
)
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_junit_rejects_dtds_before_expanding_entities(
    tmp_path: Path, declaration: str, encoding: str
) -> None:
    path = tmp_path / "junit.xml"
    path.write_text(
        f'<?xml version="1.0" encoding="{encoding}"?>{declaration}'
        '<testsuite tests="1" failures="0" errors="0" skipped="0" time="1">'
        '<testcase classname="suite" name="a"/></testsuite>',
        encoding=encoding,
    )
    with pytest.raises(ValueError, match="document type declarations are forbidden"):
        capture(path, SHA, "not live", "baseline", 1, 1)


def test_real_pytest_xdist_reports_preserve_original_node_ids_and_outcomes(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers = xdist_group: scheduling affinity\n", encoding="ascii"
    )
    (tmp_path / "test_example.py").write_text(
        "import pytest\n"
        "@pytest.mark.xdist_group('home')\n"
        "@pytest.mark.parametrize('value', ['literal@home', 'other', 'xfail', 'skip'])\n"
        "def test_case(value):\n"
        "    if value == 'skip': pytest.skip('known prerequisite')\n"
        "    if value == 'xfail': pytest.xfail('known failure')\n"
        "    assert value in ['literal@home', 'other']\n",
        encoding="ascii",
    )
    reports = []
    for variant, shard in (("baseline", 1), ("proposed", 1), ("proposed", 2)):
        xml = tmp_path / f"{variant}-{shard}.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.pytest_performance_evidence",
            "--dist",
            "loadgroup",
            "-n",
            "0" if variant == "baseline" else "2",
            f"--junitxml={xml}",
            "test_example.py",
        ]
        if variant == "proposed":
            command.extend(["--splits", "2", "--group", str(shard)])
        result = subprocess.run(
            command,
            cwd=tmp_path,
            env={**os.environ, "PYTHONPATH": str(root)},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        reports.append(
            capture(xml, SHA, "example", variant, shard, 1 if variant == "baseline" else 2)
        )
    # Ignore overhead-dominated toy timings; this case proves actual plugin interoperability.
    baseline = replace(reports[0], elapsed_seconds=10)
    proposed = [replace(report, elapsed_seconds=5) for report in reports[1:]]
    proof = compare([baseline], proposed, SHA, 0.05)
    assert proof["scenario_count"] == 4
    assert proof["outcomes"]["passed"] == 2
    assert ("pytest-nodeid", "test_example.py::test_case[literal@home]") in baseline.cases
