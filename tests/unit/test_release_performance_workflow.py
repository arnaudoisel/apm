"""Native performance evidence must use the same candidate and complete test selection."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import load_workflow, workflow_job, workflow_step

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/ci-release-performance.yml"


def _assert_probe(workflow: dict) -> None:
    assert workflow["on"] == {
        "pull_request": {"types": ["opened", "reopened", "synchronize", "labeled"]}
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is True
    for job in workflow["jobs"].values():
        assert "ci-performance" in job["if"]
        assert "secrets" not in job
        assert "environment" not in job

    candidate = workflow_job(workflow, "candidate")
    assert candidate["runs-on"] == "macos-latest"
    assert candidate["timeout-minutes"] == 15
    build = workflow_step(candidate, "Build current ARM candidate")
    assert "scripts/build-binary.sh" in build["run"]
    assert build["env"] == {"UV_FROZEN": "true", "UV_NO_SYNC": "true"}
    pack = workflow_step(candidate, "Package one immutable candidate for every variant")
    assert 'scripts/package_release.py pack --binary-name apm-darwin-arm64 --sha "$GITHUB_SHA"' in (
        " ".join(pack["run"].split())
    )
    snapshot = workflow_step(candidate, "Freeze identical cold scheduling input")
    assert snapshot["run"] == "printf '{}\\n' > .test_durations"
    uploads = [
        step["with"] for step in candidate["steps"] if "upload-artifact@" in step.get("uses", "")
    ]
    assert any(
        upload["name"] == "benchmark-arm-candidate-${{ github.run_attempt }}"
        and set(upload["path"].splitlines())
        == {"release-assets/", "scripts/package_release.py", "scripts/release-platforms.json"}
        and upload["compression-level"] == 0
        for upload in uploads
    )
    assert any(
        upload["name"] == "benchmark-arm-cold-timings-${{ github.run_attempt }}"
        and upload["path"] == ".test_durations"
        and upload["include-hidden-files"] is True
        for upload in uploads
    )

    integration = workflow_job(workflow, "integration")
    assert integration["needs"] == ["candidate"]
    assert integration["uses"] == "./.github/workflows/release-integration.yml"
    assert integration["strategy"]["fail-fast"] is False
    assert integration["strategy"]["matrix"]["include"] == [
        {"variant": "baseline", "shard": 1, "count": 1, "workers": 4},
        {"variant": "proposed", "shard": 1, "count": 2, "workers": 3},
        {"variant": "proposed", "shard": 2, "count": 2, "workers": 3},
    ]
    inputs = integration["with"]
    assert inputs["candidate-artifact-name"] == "benchmark-arm-candidate-${{ github.run_attempt }}"
    assert inputs["candidate-sha"] == "${{ github.sha }}"
    assert (
        inputs["timing-snapshot-artifact"] == "benchmark-arm-cold-timings-${{ github.run_attempt }}"
    )
    assert inputs["integration-markers"] == "not live"
    assert inputs["splitting-algorithm"] == "least_duration"
    assert inputs["shard-count"] == "${{ matrix.count }}"
    assert inputs["shard-index"] == "${{ matrix.shard }}"
    assert inputs["xdist-workers"] == "${{ matrix.workers }}"
    assert inputs["evidence-variant"] == "${{ matrix.variant }}"
    assert inputs["evidence-selection"] == "tests/integration/ -m not live"
    assert inputs["cache-apm-fetches"] is False
    assert inputs["runtime-prerequisites"] == "none"

    comparison = workflow_job(workflow, "compare")
    assert comparison["needs"] == ["candidate", "integration"]
    assert "always()" in comparison["if"] and "!cancelled()" in comparison["if"]
    require = workflow_step(comparison, "Require all benchmark executions succeeded")
    assert require["env"] == {
        "BUILD_RESULT": "${{ needs.candidate.result }}",
        "INTEGRATION_RESULT": "${{ needs.integration.result }}",
    }
    assert 'test "$BUILD_RESULT" = success' in require["run"]
    assert 'test "$INTEGRATION_RESULT" = success' in require["run"]
    compare = workflow_step(comparison, "Require identical outcomes and a measured execution gain")
    assert "scripts.compare_test_runs compare" in compare["run"]
    assert "--minimum-speedup 0.05" in compare["run"]
    assert '--expected-sha "$GITHUB_SHA"' in compare["run"]
    assert "performance-inputs/baseline/baseline-shard-1.json" in compare["run"]
    assert "performance-inputs/proposed-1/proposed-shard-1.json" in compare["run"]
    assert "performance-inputs/proposed-2/proposed-shard-2.json" in compare["run"]


def test_native_probe_uses_cold_shared_input_and_one_exact_candidate() -> None:
    _assert_probe(load_workflow(WORKFLOW))


def test_release_keeps_baseline_arm_topology_until_sharding_is_accepted() -> None:
    catalog = json.loads((ROOT / "scripts/release-platforms.json").read_text(encoding="utf-8"))
    arm = next(row for row in catalog if row["binary_name"] == "apm-darwin-arm64")
    caller = workflow_job(load_workflow(ROOT / ".github/workflows/build-release.yml"), "platforms")
    assert caller["with"]["integration-shard-count"] == "${{ matrix.integration_shard_count }}"
    assert caller["with"]["integration-xdist-workers"] == "${{ matrix.integration_xdist_workers }}"
    assert caller["with"]["integration-splitting-algorithm"] == (
        "${{ matrix.integration_splitting_algorithm }}"
    )
    assert arm["integration_shard_count"] == 1
    assert arm["integration_xdist_workers"] == 4
    assert arm["integration_splitting_algorithm"] == "duration_based_chunks"
    variants = workflow_job(load_workflow(WORKFLOW), "integration")["strategy"]["matrix"]["include"]
    baseline = next(row for row in variants if row["variant"] == "baseline")
    assert baseline["count"] == arm["integration_shard_count"]
    assert baseline["workers"] == arm["integration_xdist_workers"]


@pytest.mark.parametrize(
    "fault",
    [
        "candidate",
        "snapshot",
        "source",
        "selection",
        "workers",
        "missing-shard",
        "fail-fast",
        "flattened-archive",
    ],
)
def test_native_probe_rejects_incomparable_or_incomplete_designs(fault: str) -> None:
    workflow = deepcopy(load_workflow(WORKFLOW))
    job = workflow["jobs"]["integration"]
    if fault == "candidate":
        job["with"]["candidate-artifact-name"] = "candidate-${{ matrix.variant }}"
    elif fault == "snapshot":
        job["with"]["timing-snapshot-artifact"] = "timings-${{ matrix.variant }}"
    elif fault == "source":
        job["with"]["candidate-sha"] = "${{ github.event.pull_request.head.sha }}"
    elif fault == "selection":
        job["with"]["integration-markers"] = "lifecycle_smoke"
    elif fault == "workers":
        job["with"]["xdist-workers"] = "auto"
    elif fault == "missing-shard":
        job["strategy"]["matrix"]["include"].pop()
    elif fault == "fail-fast":
        job["strategy"]["fail-fast"] = True
    else:
        uploads = [
            step
            for step in workflow["jobs"]["candidate"]["steps"]
            if "upload-artifact@" in step.get("uses", "")
        ]
        uploads[0]["with"]["path"] = "release-assets/"
    with pytest.raises(AssertionError):
        _assert_probe(workflow)


def test_no_called_benchmark_workflow_can_request_publishing_permissions() -> None:
    pending = [WORKFLOW]
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        workflow = load_workflow(path)
        permissions = workflow.get("permissions", {})
        assert isinstance(permissions, dict), path
        assert all(value in ("read", "none") for value in permissions.values()), path
        for job in workflow["jobs"].values():
            permissions = job.get("permissions", {})
            assert isinstance(permissions, dict), path
            assert all(value in ("read", "none") for value in permissions.values()), path
            assert "environment" not in job, path
            assert "secrets" not in job, path
            target = job.get("uses", "")
            if target.startswith("./.github/workflows/"):
                pending.append(ROOT / target.removeprefix("./"))
