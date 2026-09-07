"""Semantic contracts for parallel, native, exact-artifact release evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

from tests.workflow_contracts import (
    assert_exact_command,
    assert_unconditional,
    effective_env,
    load_workflow,
    shell_commands,
    shell_tokens,
    workflow_job,
    workflow_step,
    workflow_step_index,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/release-platform.yml"
RELEASE_WORKFLOW = ROOT / ".github/workflows/build-release.yml"
UNIX_TIMEOUT = "${{ inputs.integration-markers == 'lifecycle_smoke and not live' && 30 || 60 }}"
UNIX_ARGS = (
    "-n 4 --dist loadgroup --durations=50 --store-durations --junitxml=test-results/integration.xml"
)
SMOKE = "Test native binary startup and core contracts"
INSTALLER = "Test install.ps1 end-to-end (Windows)"


def _workflow() -> dict:
    return load_workflow(WORKFLOW)


def test_candidate_source_authorities_match_the_real_reusable_workflow() -> None:
    """Reject invented job names and omitted authorities in promotion evidence."""
    source = load_workflow(ROOT / ".github/workflows/ci.yml")
    expected = set()
    for job_id, job in source["jobs"].items():
        if job_id == "pr-binary-smoke":
            continue
        name = job["name"]
        shards = job.get("strategy", {}).get("matrix", {}).get("shard", [None])
        for shard in shards:
            expected.add(
                "Candidate Source Checks / " + name.replace("${{ matrix.shard }}", str(shard))
            )
    completed = subprocess.run(
        [
            "node",
            "-e",
            "process.stdout.write(JSON.stringify("
            "require('./scripts/release-candidate.cjs').REQUIRED_SOURCE_JOB_NAMES));",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    names = json.loads(completed.stdout)
    assert len(names) == len(set(names))
    assert set(names) == expected


def _execute_native_gate(results: dict, full: bool, platform: str) -> list[str]:
    gate = workflow_job(_workflow(), "gate")
    script = workflow_step(gate, "Require every applicable native result")["with"]["script"]
    harness = (
        "const failures = [];"
        "const core = {setFailed: message => failures.push(message)};"
        f"(new Function('core', {json.dumps(script)}))(core);"
        "process.stdout.write(JSON.stringify(failures));"
    )
    completed = subprocess.run(
        ["node", "-e", harness],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
        env={
            **os.environ,
            "RESULTS": json.dumps(results),
            "FULL": str(full).lower(),
            "PLATFORM": platform,
        },
    )
    return json.loads(completed.stdout)


@pytest.mark.parametrize(
    "job_name",
    ["unit-tests", "build", "integration-tests", "release-validation", "windows-installer"],
)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "neutral", None])
def test_native_gate_executes_fail_closed_for_each_required_job(
    job_name: str, result: str | None
) -> None:
    gate = workflow_job(_workflow(), "gate")
    results = {name: {"result": "success"} for name in gate["needs"]}
    if result is None:
        del results[job_name]
    else:
        results[job_name]["result"] = result
    assert _execute_native_gate(results, True, "windows") == [f"{job_name} did not succeed"]


@pytest.mark.parametrize("full", [False, True])
@pytest.mark.parametrize("platform", ["linux", "darwin", "windows"])
def test_native_gate_accepts_only_applicable_successes(full: bool, platform: str) -> None:
    gate = workflow_job(_workflow(), "gate")
    results = {name: {"result": "success"} for name in gate["needs"]}
    if not full:
        results["integration-tests"]["result"] = "skipped"
        results["release-validation"]["result"] = "skipped"
    if platform != "windows":
        results["windows-installer"]["result"] = "skipped"
    assert _execute_native_gate(results, full, platform) == []


def _assert_native_startup(workflow: dict) -> None:
    job = workflow_job(workflow, "build")
    assert_unconditional(job, label="native candidate build")
    assert job["runs-on"] == "${{ inputs.runner }}"
    step = workflow_step(job, SMOKE)
    assert_unconditional(step, label="native artifact smoke")
    assert effective_env(workflow, job, step).get("GITHUB_TOKEN") is None
    assert step["env"] == {
        "PYTHONUTF8": "1",
        "APM_E2E_TESTS": "1",
        "APM_BINARY_PATH": (
            "${{ github.workspace }}/dist/${{ inputs.binary-name }}/"
            "${{ inputs.platform == 'windows' && 'apm.exe' || 'apm' }}"
        ),
    }
    assert_exact_command(
        shell_commands(step),
        [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "tests/integration/test_core_smoke.py",
            "-vv",
            "-ra",
            "--tb=short",
            "--junitxml=test-results/core.xml",
        ],
        label="native core smoke",
    )
    for build in ("Build binary (Unix)", "Build binary (Windows)"):
        assert workflow_step_index(job, build) < workflow_step_index(job, SMOKE)
    assert workflow_step_index(job, SMOKE) < workflow_step_index(
        job, "Package exact candidate archive"
    )
    assert workflow_step_index(job, "Package exact candidate archive") < workflow_step_index(
        job, "Upload binary as workflow artifact"
    )


def _assert_windows_installer(workflow: dict) -> None:
    job = workflow_job(workflow, "windows-installer")
    assert job["needs"] == ["build"]
    assert job["if"] == "inputs.platform == 'windows'"
    step = workflow_step(job, INSTALLER)
    env = effective_env(workflow, job, step)
    assert env.get("GITHUB_TOKEN") is None
    assert env.get("GH_TOKEN") is None
    assert step["env"]["APM_E2E_TESTS"] == "1"
    assert step["env"]["APM_CANDIDATE_ARCHIVE"] == (
        "${{ github.workspace }}/release-assets/apm-windows-x86_64.zip"
    )
    assert step["env"]["APM_BASELINE_ARCHIVE"] == (
        "${{ github.workspace }}/installer-baseline/apm-windows-x86_64.zip"
    )
    assert env["APM_BASELINE_VERSION"] == "v0.28.0"
    assert "APM_CANDIDATE_VERSION" in step["run"]
    assert "APM_CANDIDATE_SHA256" in step["run"]
    baseline = workflow_step(job, "Download historical upgrade source once")
    assert baseline["env"] == {"GH_TOKEN": "${{ github.token }}"}
    assert "gh release download $env:APM_BASELINE_VERSION" in baseline["run"]
    assert "if ($actual -ne $expected)" in baseline["run"]
    assert workflow_step_index(job, "Download historical upgrade source once") < (
        workflow_step_index(job, INSTALLER)
    )
    assert_exact_command(
        shell_commands(step),
        [
            "uv",
            "run",
            "--frozen",
            "pytest",
            "tests/integration/test_windows_installer_launchers.py",
            "-vv",
            "-ra",
            "--tb=short",
            "--junitxml=test-results/installer.xml",
        ],
        label="candidate Windows installer",
    )


def test_installer_workflow_inputs_reach_the_actual_candidate_reader(tmp_path: Path) -> None:
    """Catch producer/consumer naming drift, not just two independent string checks."""
    from tests.utils.windows_installer_candidate import EXECUTABLE_MEMBER, InstallerArchive

    archive = tmp_path / "candidate.zip"
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(EXECUTABLE_MEMBER, b"candidate")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    workflow = _workflow()
    job = workflow_job(workflow, "windows-installer")
    step = workflow_step(job, INSTALLER)
    environment = dict(effective_env(workflow, job, step))
    for name in environment:
        if "CANDIDATE" in name and name.endswith("_ARCHIVE"):
            environment[name] = str(archive)
    for name in re.findall(r"\$env:(APM_[A-Z0-9_]+)\s*=", step["run"]):
        if name.endswith("_VERSION"):
            environment[name] = "v9.8.7"
        elif name.endswith("_SHA256"):
            environment[name] = digest
    candidate = InstallerArchive.from_environment(environment, "CANDIDATE")
    assert candidate.sha256 == digest
    assert candidate.version == "v9.8.7"


def _assert_parallel_paths(workflow: dict) -> None:
    for name in ("unit-tests", "build"):
        assert "needs" not in workflow_job(workflow, name)
    for name in ("integration-tests", "release-validation", "windows-installer"):
        assert workflow_job(workflow, name)["needs"] == ["build"]
    gate = workflow_job(workflow, "gate")
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == {
        "unit-tests",
        "build",
        "integration-tests",
        "release-validation",
        "windows-installer",
    }
    step = workflow_step(gate, "Require every applicable native result")
    assert step["env"]["RESULTS"] == "${{ toJSON(needs) }}"
    assert "results[name]?.result !== 'success'" in step["with"]["script"]
    assert "core.setFailed" in step["with"]["script"]


def _assert_integration(workflow: dict) -> None:
    job = workflow_job(workflow, "integration-tests")
    unix = workflow_step(job, "Run integration tests (Unix)")
    assert unix["env"]["PYTEST_MARK_EXPR"] == "${{ inputs.integration-markers }}"
    assert unix["env"]["PYTEST_EXTRA_ARGS"] == UNIX_ARGS
    assert unix["timeout-minutes"] == UNIX_TIMEOUT
    assert unix["env"]["APM_BINARY_PATH"] == (
        "${{ github.workspace }}/dist/${{ inputs.binary-name }}/apm"
    )
    windows = workflow_step(job, "Run integration tests (Windows)")
    assert windows["timeout-minutes"] == 20
    assert windows["env"]["APM_BINARY_PATH"].endswith("/apm.exe")
    for step in (unix, windows):
        assert step["env"]["GITHUB_API_TOKEN"] == "${{ github.token }}"
        assert step["env"]["GITHUB_APM_PAT"] == "${{ secrets.GH_CLI_PAT }}"
        assert "APM_RUN_INFERENCE_TESTS" not in effective_env(workflow, job, step)
    assert "-IncludeLiveADO" not in shell_tokens(windows)


def test_native_startup_runs_exact_frozen_candidate_before_packaging() -> None:
    _assert_native_startup(_workflow())


def test_windows_installer_is_candidate_bound_and_tokenless() -> None:
    _assert_windows_installer(_workflow())


def test_native_jobs_have_no_unit_or_cross_platform_barriers() -> None:
    _assert_parallel_paths(_workflow())


def test_native_integration_preserves_grouping_bounds_and_artifact_identity() -> None:
    _assert_integration(_workflow())


def test_platform_catalog_retains_all_native_and_non_live_selections() -> None:
    catalog = json.loads((ROOT / "scripts/release-platforms.json").read_text("ascii"))
    rows = {row["binary_name"]: row for row in catalog}
    assert len(rows) == len(catalog) == 5
    assert {name: row["runner"] for name, row in rows.items()} == {
        "apm-linux-x86_64": "ubuntu-24.04",
        "apm-linux-arm64": "ubuntu-24.04-arm",
        "apm-darwin-x86_64": "macos-15-intel",
        "apm-darwin-arm64": "macos-latest",
        "apm-windows-x86_64": "windows-latest",
    }
    assert rows["apm-darwin-x86_64"]["integration_markers"] == "lifecycle_smoke and not live"
    assert all(
        row["integration_markers"] == "not live"
        for name, row in rows.items()
        if name != "apm-darwin-x86_64"
    )
    assert {name for name, row in rows.items() if not row["on_main"]} == {"apm-darwin-arm64"}


def test_isolated_validation_has_no_checkout_and_uses_checked_archive() -> None:
    workflow = _workflow()
    job = workflow_job(workflow, "release-validation")
    assert all("actions/checkout" not in step.get("uses", "") for step in job["steps"])
    unpack = workflow_step(job, "Extract checked candidate archive")
    assert "verify-extract" in shell_tokens(unpack)
    for name in ("Run release validation tests (Unix)", "Run release validation tests (Windows)"):
        step = workflow_step(job, name)
        assert step["timeout-minutes"] == 20
        env = effective_env(workflow, job, step)
        assert "GITHUB_TOKEN" not in env
        assert "APM_RUN_INFERENCE_TESTS" not in env


@pytest.mark.parametrize("job", ["unit-tests", "build"])
def test_unit_build_dependency_mutation_is_rejected(job: str) -> None:
    workflow = deepcopy(_workflow())
    workflow_job(workflow, job)["needs"] = ["another-platform"]
    with pytest.raises(AssertionError):
        _assert_parallel_paths(workflow)


@pytest.mark.parametrize("job", ["integration-tests", "release-validation", "windows-installer"])
def test_reintroduced_barrier_is_rejected(job: str) -> None:
    workflow = deepcopy(_workflow())
    workflow_job(workflow, job)["needs"].append("unit-tests")
    with pytest.raises(AssertionError):
        _assert_parallel_paths(workflow)


@pytest.mark.parametrize("scope", ["workflow", "job", "step"])
@pytest.mark.parametrize("contract", ["smoke", "installer"])
def test_native_token_scope_mutations_are_rejected(scope: str, contract: str) -> None:
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, "build" if contract == "smoke" else "windows-installer")
    step = workflow_step(job, SMOKE if contract == "smoke" else INSTALLER)
    {"workflow": workflow, "job": job, "step": step}[scope].setdefault("env", {})[
        "GITHUB_TOKEN"
    ] = "secret"
    with pytest.raises(AssertionError):
        (_assert_native_startup if contract == "smoke" else _assert_windows_installer)(workflow)


@pytest.mark.parametrize("scope", ["job", "step"])
def test_disabling_native_startup_is_rejected(scope: str) -> None:
    workflow = deepcopy(_workflow())
    job = workflow_job(workflow, "build")
    {"job": job, "step": workflow_step(job, SMOKE)}[scope]["if"] = False
    with pytest.raises(AssertionError):
        _assert_native_startup(workflow)


def test_echo_cannot_replace_frozen_smoke() -> None:
    workflow = deepcopy(_workflow())
    step = workflow_step(workflow_job(workflow, "build"), SMOKE)
    step["run"] = "echo " + step["run"]
    with pytest.raises(AssertionError):
        _assert_native_startup(workflow)


@pytest.mark.parametrize("field", ["PYTEST_MARK_EXPR", "PYTEST_EXTRA_ARGS", "APM_BINARY_PATH"])
def test_missing_integration_safety_argument_is_rejected(field: str) -> None:
    workflow = deepcopy(_workflow())
    step = workflow_step(
        workflow_job(workflow, "integration-tests"), "Run integration tests (Unix)"
    )
    step["env"][field] = "wrong"
    with pytest.raises(AssertionError):
        _assert_integration(workflow)


def test_publication_only_consumes_verified_archives() -> None:
    workflow = load_workflow(RELEASE_WORKFLOW)
    publisher = workflow_job(workflow, "create-release")
    assert publisher["needs"] == ["plan", "verify-candidate"]
    assert all("run" not in step for step in publisher["steps"])
    assert all("actions/checkout" not in step.get("uses", "") for step in publisher["steps"])
    assert workflow_step(publisher, "Create GitHub Release")["with"]["fail_on_unmatched_files"]


def test_native_artifacts_and_downloads_are_attempt_scoped() -> None:
    workflow = _workflow()
    artifact_name = "candidate-${{ github.run_attempt }}-${{ inputs.binary-name }}"
    upload = workflow_step(workflow_job(workflow, "build"), "Upload binary as workflow artifact")
    assert upload["with"]["name"] == artifact_name
    for name in ("integration-tests", "release-validation", "windows-installer"):
        downloads = [
            step
            for step in workflow_job(workflow, name)["steps"]
            if step.get("uses", "").startswith("actions/download-artifact@")
        ]
        assert len(downloads) == 1
        assert downloads[0]["with"]["name"] == artifact_name


def test_tag_downloads_only_exact_selected_artifact_ids() -> None:
    workflow = load_workflow(RELEASE_WORKFLOW)
    verifier = workflow_job(workflow, "verify-candidate")
    for name in ("Download qualified evidence", "Download exact candidate artifacts"):
        download = workflow_step(verifier, name)
        assert "artifact-ids" in download["with"]
        assert "pattern" not in download["with"]
        assert "name" not in download["with"]
        assert workflow_step_index(verifier, "Require exact immutable download identities") < (
            workflow_step_index(verifier, name)
        )
        assert download["with"]["run-id"] == (
            "${{ needs.plan.outputs.candidate_run_id || github.run_id }}"
        )
    qualifier = workflow_job(workflow, "candidate-ready")
    assert qualifier["outputs"]["candidate_artifact_ids"] == (
        "${{ steps.qualification.outputs.candidate_artifact_ids }}"
    )
    evidence = next(step for step in qualifier["steps"] if step.get("id") == "evidence")
    assert evidence["with"]["name"] == "release-candidate-evidence-${{ github.run_attempt }}"


@pytest.mark.parametrize(
    ("job_name", "authority"),
    [
        ("create-release", "verify-candidate"),
        ("deploy-docs", "create-release"),
        ("gh-aw-compat", "create-release"),
        ("build-pypi-distributions", "create-release"),
        ("publish-pypi", "build-pypi-distributions"),
    ],
)
def test_warm_release_descendants_require_success_without_skipped_ancestor_poisoning(
    job_name: str, authority: str
) -> None:
    job = workflow_job(load_workflow(RELEASE_WORKFLOW), job_name)
    assert authority in job["needs"]
    assert "always()" in job["if"]
    assert "!cancelled()" in job["if"]
    assert f"needs.{authority}.result == 'success'" in job["if"]
