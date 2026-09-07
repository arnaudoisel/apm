"""A whole-release experiment must execute the existing authorities without publication."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import load_workflow, workflow_job, workflow_step

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci-release-rehearsal.yml"


def _assert_rehearsal(workflow: dict) -> None:
    assert workflow["on"] == {
        "pull_request": {"types": ["opened", "reopened", "synchronize", "labeled"]},
    }
    assert workflow["permissions"] == {"contents": "read", "actions": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    assert "ci-release-rehearsal" in workflow["jobs"]["plan"]["if"]
    for job in workflow["jobs"].values():
        assert "secrets" not in job
        assert "environment" not in job
        assert all(
            permission in {"read", "none"} for permission in job.get("permissions", {}).values()
        )

    plan = workflow_job(workflow, "plan")
    matrix = workflow_step(plan, "Derive all five native controls and the three paired surfaces")
    assert matrix["run"] == 'python -m scripts.release_rehearsal matrix >> "$GITHUB_OUTPUT"'
    platforms = workflow_job(workflow, "platforms")
    assert platforms["uses"] == "./.github/workflows/release-platform.yml"
    assert platforms["name"] == "${{ matrix.binary_name }}"
    assert platforms["needs"] == ["plan"]
    assert platforms["strategy"]["fail-fast"] is False
    assert platforms["strategy"]["matrix"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
    assert platforms["with"]["full-validation"] is True
    for argument, field in (
        ("unit-performance-probe", "unit_performance_probe"),
        ("integration-performance-probe", "integration_performance_probe"),
        ("performance-workers", "performance_workers"),
        ("integration-markers", "integration_markers"),
        ("integration-shard-count", "integration_shard_count"),
        ("integration-xdist-workers", "integration_xdist_workers"),
    ):
        assert platforms["with"][argument] == "${{ matrix." + field + " }}"

    source = workflow_job(workflow, "source-checks")
    assert source["name"] == "Candidate Source Checks"
    assert source["uses"] == "./.github/workflows/ci.yml"
    assert source["needs"] == ["plan"]
    for key, builder in (("docs", "docs-build"), ("wheels", "pypi-distributions")):
        job = workflow_job(workflow, key)
        assert job["uses"] == f"./.github/workflows/{builder}.yml"
        assert job["needs"] == ["plan"]

    report = workflow_job(workflow, "compare")
    assert report["needs"] == ["plan", "source-checks", "platforms", "docs", "wheels"]
    assert "always()" in report["if"] and "!cancelled()" in report["if"]
    collect = workflow_step(
        report, "Record current-attempt job timestamps and require production authorities"
    )
    script = collect["with"]["script"]
    assert "listJobsForWorkflowRunAttempt" in script
    assert "attempt_number: attempt" in script
    assert "selectRequiredJobs(jobs, catalog)" in script
    assert "source_sha: context.sha" in script
    assert "run_id: String(context.runId), run_attempt: attempt" in script
    check = workflow_step(report, "Require original outcomes and measure the entire critical path")
    assert "--minimum-lane-gain 0.15 --minimum-overall-gain 0.10" in check["run"]
    assert '--expected-sha "$GITHUB_SHA"' in check["run"]
    downloads = [
        step["with"]["pattern"]
        for step in report["steps"]
        if "download-artifact@" in step.get("uses", "")
    ]
    assert len(downloads) == 5
    assert all("${{ github.run_attempt }}" in pattern for pattern in downloads)
    assert downloads[:2] == [
        "performance-evidence-${{ github.run_attempt }}-apm-*",
        "performance-evidence-unit-${{ github.run_attempt }}-apm-*",
    ]
    for surface in (
        "integration-apm-darwin-arm64",
        "integration-apm-linux-x86_64",
        "unit-apm-darwin-x86_64",
    ):
        suite, binary = surface.split("-apm-", 1)
        assert (
            f"performance-cohort-{suite}-${{{{ github.run_attempt }}}}-apm-{binary}-*" in downloads
        )


def test_read_only_rehearsal_reuses_full_native_source_and_publication_build_authorities() -> None:
    _assert_rehearsal(load_workflow(WORKFLOW))


@pytest.mark.parametrize(
    "fault",
    [
        "privileged",
        "secrets",
        "partial-validation",
        "missing-source",
        "fail-fast",
        "worker-drift",
        "old-artifacts",
        "missing-lane",
        "missing-authority",
        "weaker-threshold",
    ],
)
def test_rehearsal_cannot_silently_remove_controls_or_accept_weaker_proof(fault: str) -> None:
    workflow = deepcopy(load_workflow(WORKFLOW))
    jobs = workflow["jobs"]
    if fault == "privileged":
        workflow["permissions"]["contents"] = "write"
    elif fault == "secrets":
        jobs["platforms"]["secrets"] = "inherit"
    elif fault == "partial-validation":
        jobs["platforms"]["with"]["full-validation"] = False
    elif fault == "missing-source":
        jobs["source-checks"]["uses"] = "./.github/workflows/ci-source-performance.yml"
    elif fault == "fail-fast":
        jobs["platforms"]["strategy"]["fail-fast"] = True
    elif fault == "worker-drift":
        jobs["platforms"]["with"]["performance-workers"] = 99
    elif fault == "missing-lane":
        jobs["compare"]["needs"].remove("platforms")
    elif fault == "missing-authority":
        step = workflow_step(
            jobs["compare"],
            "Record current-attempt job timestamps and require production authorities",
        )
        step["with"]["script"] = step["with"]["script"].replace(
            "selectRequiredJobs(jobs, catalog)", "null"
        )
    elif fault == "weaker-threshold":
        step = workflow_step(
            jobs["compare"], "Require original outcomes and measure the entire critical path"
        )
        step["run"] = step["run"].replace("--minimum-overall-gain 0.10", "--minimum-overall-gain 0")
    else:
        step = workflow_step(jobs["compare"], "Download current integration proofs")
        step["with"]["pattern"] = "performance-evidence-*"
    with pytest.raises(AssertionError):
        _assert_rehearsal(workflow)


def test_cohort_barrier_is_before_pytest_and_static_callers_grant_read_only_actions() -> None:
    integration = load_workflow(ROOT / ".github/workflows/release-integration.yml")
    steps = integration["jobs"]["integration-tests"]["steps"]
    barrier = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses") == "./.github/actions/performance-cohort"
    )
    execution = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Run integration tests (Unix)"
    )
    assert barrier < execution
    assert steps[barrier]["if"] == "inputs.performance-cohort != ''"
    assert integration["permissions"] == {"contents": "read", "actions": "read"}
    for file in ("build-release.yml", "ci-integration.yml", "ci-release-performance.yml"):
        workflow = load_workflow(ROOT / ".github/workflows" / file)
        callers = [
            job
            for job in workflow["jobs"].values()
            if job.get("uses")
            in {
                "./.github/workflows/release-integration.yml",
                "./.github/workflows/release-platform.yml",
            }
        ]
        assert callers
        assert all(job["permissions"] == {"contents": "read", "actions": "read"} for job in callers)
