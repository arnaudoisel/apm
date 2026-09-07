"""Whole-path claims must retain native floors and never add parallel savings."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.compare_test_runs import Report
from scripts.package_release import BINARY_NAMES
from scripts.release_rehearsal import ROOT, SURFACES, evaluate, rehearsal_matrix, source_authorities

pytestmark = pytest.mark.component
SHA = "a" * 40
ORIGIN = datetime(2026, 9, 7, tzinfo=timezone.utc)


def _at(seconds: float) -> str:
    return (ORIGIN + timedelta(seconds=seconds)).isoformat()


@pytest.fixture(scope="module")
def source_names() -> list[str]:
    return source_authorities()


@pytest.fixture
def evidence(tmp_path: Path, source_names: list[str]) -> tuple:
    jobs = []

    def job(name: str, end: float, runner: str = "") -> dict:
        entry = {
            "id": len(jobs) + 1,
            "run_id": 123,
            "run_attempt": 1,
            "name": name,
            "runner_name": runner or f"runner-{len(jobs) + 1}",
            "status": "completed",
            "conclusion": "success",
            "started_at": _at(1),
            "completed_at": _at(end),
            "steps": [],
        }
        jobs.append(entry)
        return entry

    for name in source_names:
        job(name, 100)
    for binary in BINARY_NAMES:
        integration_end = {"apm-darwin-arm64": 1010, "apm-linux-x86_64": 810}.get(binary, 310)
        native_end = {
            "apm-darwin-arm64": 1020,
            "apm-linux-x86_64": 820,
            "apm-darwin-x86_64": 920,
            "apm-windows-x86_64": 410,
        }.get(binary, 320)
        job(f"{binary} / Native Candidate Gate", native_end)
        job(f"{binary} / Integration Tests", integration_end)
        job(f"{binary} / Build Candidate", 30)
        job(f"{binary} / Isolated Release Validation", 50)
        if binary != "apm-darwin-x86_64":
            job(f"{binary} / Unit Tests / Unit Tests Baseline", 200)
        else:
            job(f"{binary} / Unit Tests / Unit Performance Probe", 915)
        if binary not in {"apm-darwin-arm64", "apm-linux-x86_64"}:
            job(f"{binary} / Integration Tests Shard 1 / Integration Tests Shard 1", 300)
        if binary == "apm-windows-x86_64":
            job(f"{binary} / Candidate Windows Installer", 400)
    job("Read-only documentation build / build", 70)
    job("Read-only Python distribution build / Build PyPI Distributions", 70)
    reports = []
    ends = {
        "arm-integration": (1000, 500, 520),
        "linux-integration": (800, 450, 470),
        "intel-units": (900, 420, 440),
    }
    for surface in SURFACES:
        candidate = (
            None
            if surface.suite == "unit"
            else {
                "schema_version": 1,
                "sha": SHA,
                "version": "0.30.0",
                "binary_name": surface.binary,
                "archive": surface.binary + ".tar.gz",
                "archive_sha256": "b" * 64,
                "executable_sha256": "c" * 64,
            }
        )
        identity = {
            "source_sha": SHA,
            "selection": surface.selection,
            "candidate": candidate,
            "environment": {
                "python": "3.12.10",
                "system": "Darwin" if "darwin" in surface.binary else "Linux",
                "release": "release",
                "machine": surface.binary.rsplit("-", 1)[1],
                "cpu_count": surface.cpus,
                "runner_image": "20260831.1",
                "dependency_lock": "f" * 64,
            },
        }
        cases = {("pytest-nodeid", "test_a"): "passed", ("pytest-nodeid", "test_b"): "skipped"}
        root = tmp_path / surface.key
        root.mkdir()
        for index, member in enumerate(("baseline-1", "proposed-1", "proposed-2")):
            variant, shard = member.split("-")
            runner = f"{surface.key}-{member}"
            job(
                f"{surface.binary} / {surface.suite} experiment {member}",
                ends[surface.key][index],
                runner,
            )
            reports.append(
                Report(
                    deepcopy(identity),
                    variant,
                    int(shard),
                    1 if variant == "baseline" else 2,
                    ends[surface.key][index] - 40,
                    cases if index == 0 else dict([list(cases.items())[index - 1]]),
                )
            )
            record = {
                "schema_version": 1,
                "run_id": "123",
                "run_attempt": 1,
                "cohort": f"{surface.suite}-1-{surface.binary}",
                "member": member,
                "identity": identity,
                "execution": {
                    "runner_name": runner,
                    "workflow_job": "tests",
                    "recorded_at": _at(20),
                },
            }
            (root / f"{member}.json").write_text(json.dumps(record), encoding="ascii")
    metadata = {
        "schema_version": 1,
        "source_sha": SHA,
        "run_id": "123",
        "run_attempt": 1,
        "created_at": _at(0),
        "run_started_at": _at(0),
        "jobs": jobs,
    }
    return metadata, reports, tmp_path


def _evaluate(evidence: tuple, source_names: list[str]) -> dict:
    metadata, reports, root = evidence
    return evaluate(metadata, reports, root, SHA, source_names, 0.15, 0.10)


def test_complete_model_uses_maxima_and_reveals_the_next_bottleneck(
    evidence: tuple,
    source_names: list[str],
) -> None:
    result = _evaluate(evidence, source_names)
    scenarios = result["scenarios"]
    assert scenarios["baseline"]["evidence_ready_seconds"] == 1020
    assert scenarios["arm_only"]["evidence_ready_seconds"] == 905
    assert "apm-darwin-x86_64" in scenarios["arm_only"]["limiting_job"]
    assert scenarios["arm_and_linux"]["evidence_ready_seconds"] == 905
    assert scenarios["combined"]["evidence_ready_seconds"] == 540
    assert result["saved_seconds"] == 480
    assert result["overall_gain_fraction"] == pytest.approx(480 / 1020)
    assert result["reaches_30_percent_target"] is True
    assert result["unchanged_tail_sensitivity"][-1]["gain_fraction"] == pytest.approx(480 / 1200)
    authorities = scenarios["combined"]["native_authorities"]
    assert len(authorities) == 5
    arm = next(item for item in authorities if item["integration_surface"] == "arm-integration")
    assert arm["integration_fan_in_delay_seconds"] == 10
    assert arm["native_gate_delay_seconds"] == 10
    assert arm["modeled_ready_seconds"] == 540


def test_late_required_native_authorities_cannot_be_dropped_to_manufacture_a_win(
    evidence: tuple,
    source_names: list[str],
) -> None:
    metadata, _, _ = evidence
    for job in metadata["jobs"]:
        if job["name"].endswith("Native Candidate Gate"):
            job["completed_at"] = _at(1100)
    result = _evaluate(evidence, source_names)
    assert result["scenarios"]["baseline"]["evidence_ready_seconds"] == 1100
    assert result["scenarios"]["combined"]["evidence_ready_seconds"] == 1100
    assert result["overall_gain_fraction"] == 0
    assert result["meets_minimum"] is False


@pytest.mark.parametrize("fault", ["missing", "failed", "before-prerequisites"])
def test_native_fan_in_evidence_is_required_and_must_follow_its_inputs(
    evidence: tuple,
    source_names: list[str],
    fault: str,
) -> None:
    metadata, _, _ = evidence
    gate = next(
        job for job in metadata["jobs"] if job["name"] == "apm-darwin-arm64 / Integration Tests"
    )
    if fault == "missing":
        metadata["jobs"].remove(gate)
    elif fault == "failed":
        gate["conclusion"] = "failure"
    else:
        gate["completed_at"] = _at(500)
    with pytest.raises(ValueError):
        _evaluate(evidence, source_names)


def test_fast_shards_do_not_imply_an_overall_gain_when_an_unchanged_job_dominates(
    evidence: tuple,
    source_names: list[str],
) -> None:
    metadata, _, _ = evidence
    installer = next(
        job for job in metadata["jobs"] if job["name"].endswith("Candidate Windows Installer")
    )
    installer["completed_at"] = _at(1200)
    gate = next(
        job
        for job in metadata["jobs"]
        if job["name"] == "apm-windows-x86_64 / Native Candidate Gate"
    )
    gate["completed_at"] = _at(1210)
    result = _evaluate(evidence, source_names)
    assert result["meets_lane_thresholds"] is True
    assert result["overall_gain_fraction"] == 0
    assert result["meets_minimum"] is False
    assert result["scenarios"]["combined"]["limiting_job"] == gate["name"]


@pytest.mark.parametrize(
    "fault", ["missing", "failed", "skipped", "duplicate", "old-attempt", "old-run", "bad-time"]
)
def test_rejects_incomplete_or_invalid_common_authorities(
    evidence: tuple,
    source_names: list[str],
    fault: str,
) -> None:
    metadata, _, _ = evidence
    job = metadata["jobs"][0]
    if fault == "missing":
        metadata["jobs"].pop(0)
    elif fault == "duplicate":
        metadata["jobs"].append(deepcopy(job))
    elif fault == "old-attempt":
        job["run_attempt"] = 2
    elif fault == "old-run":
        job["run_id"] = 124
    elif fault == "bad-time":
        job["completed_at"] = _at(-1)
    else:
        job["conclusion"] = fault
    with pytest.raises(ValueError):
        _evaluate(evidence, source_names)


@pytest.mark.parametrize(
    "fault",
    ["missing-shard", "new-skip", "postflight-drift", "wrong-capacity", "timing-outside-job"],
)
def test_rejects_invalid_paired_proof_without_weakening_the_existing_checker(
    evidence: tuple,
    source_names: list[str],
    fault: str,
) -> None:
    _, reports, _ = evidence
    if fault == "missing-shard":
        reports.pop()
    elif fault == "new-skip":
        reports[1] = replace(reports[1], cases={("pytest-nodeid", "test_a"): "skipped"})
    elif fault == "timing-outside-job":
        reports[1] = replace(reports[1], elapsed_seconds=10000)
    else:
        for index in range(3):
            identity = deepcopy(reports[index].identity)
            identity["environment"][
                "runner_image" if fault == "postflight-drift" else "cpu_count"
            ] = "different"
            reports[index] = replace(reports[index], identity=identity)
    with pytest.raises(ValueError):
        _evaluate(evidence, source_names)


def test_cannot_assign_a_cohort_member_to_a_different_job(
    evidence: tuple,
    source_names: list[str],
) -> None:
    _, _, root = evidence
    path = root / "arm-integration/proposed-1.json"
    data = json.loads(path.read_text("ascii"))
    data["execution"]["runner_name"] = "not-an-observed-runner"
    path.write_text(json.dumps(data), encoding="ascii")
    with pytest.raises(ValueError, match="uniquely bind"):
        _evaluate(evidence, source_names)


def test_baseline_native_gate_still_must_succeed(
    evidence: tuple,
    source_names: list[str],
) -> None:
    metadata, _, _ = evidence
    gate = next(job for job in metadata["jobs"] if job["name"].endswith("Native Candidate Gate"))
    gate["conclusion"] = "failure"
    with pytest.raises(ValueError, match="did not succeed"):
        _evaluate(evidence, source_names)


def test_matrix_preserves_every_platform_and_changes_only_opt_in_inputs() -> None:
    catalog = json.loads((ROOT / "scripts/release-platforms.json").read_text("ascii"))
    before = deepcopy(catalog)
    matrix = rehearsal_matrix(catalog)["include"]
    assert catalog == before
    assert len(matrix) == 5
    assert {row["binary_name"] for row in matrix if row["unit_performance_probe"]} == {
        "apm-darwin-x86_64"
    }
    assert {row["binary_name"] for row in matrix if row["integration_performance_probe"]} == {
        "apm-darwin-arm64",
        "apm-linux-x86_64",
    }
    assert all(row["integration_shard_count"] == 1 for row in matrix)
