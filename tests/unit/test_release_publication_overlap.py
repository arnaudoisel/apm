"""Executable contracts for read-only release publication prebuild overlap."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from tests.workflow_contracts import load_workflow, workflow_job, workflow_step

pytestmark = pytest.mark.component
ROOT = Path(__file__).resolve().parents[2]
BUILD_RELEASE = ROOT / ".github" / "workflows" / "build-release.yml"
DOCS = ROOT / ".github" / "workflows" / "docs.yml"
DOCS_BUILD = ROOT / ".github" / "workflows" / "docs-build.yml"
PYPI_DISTRIBUTIONS = ROOT / ".github" / "workflows" / "pypi-distributions.yml"
DOCS_ARTIFACT = "docs-pages-${{ github.run_attempt }}"
PYPI_ARTIFACT = "pypi-distributions-${{ github.run_attempt }}"
VERIFIED_RELEASE_ARTIFACT = "verified-release-assets-${{ github.run_attempt }}"


def _workflow(path: Path) -> dict[str, Any]:
    """Load a workflow as a mutable mapping."""
    return load_workflow(path)


def _replace_outside_strings(expression: str, pattern: str, replacement: str) -> str:
    """Apply a regex replacement only outside single-quoted expression strings."""
    parts = re.split(r"('[^']*')", expression)
    for index in range(0, len(parts), 2):
        parts[index] = re.sub(pattern, replacement, parts[index])
    return "".join(parts)


def _expression_value(
    expression: str,
    *,
    github: dict[str, Any],
    needs: dict[str, dict[str, Any]],
    inputs: dict[str, Any] | None = None,
    cancelled: bool = False,
) -> bool:
    """Evaluate the GitHub expression subset used by the release DAG."""
    rendered = " ".join(expression.split())
    if rendered.startswith("${{") and rendered.endswith("}}"):
        rendered = rendered.removeprefix("${{").removesuffix("}}").strip()
    replacements = {
        "github.event_name": github.get("event_name"),
        "github.ref_type": github.get("ref_type"),
        "github.event.repository.private": github.get("private"),
        "github.event.release.prerelease": github.get("release_prerelease"),
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, repr(value))
    for match in set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.result", rendered)):
        rendered = rendered.replace(
            f"needs.{match}.result",
            repr(needs.get(match, {}).get("result")),
        )
    for job, output in set(
        re.findall(r"needs\.([A-Za-z0-9_-]+)\.outputs\.([A-Za-z0-9_-]+)", rendered)
    ):
        value = needs.get(job, {}).get("outputs", {}).get(output)
        rendered = rendered.replace(f"needs.{job}.outputs.{output}", repr(value))
    for name in set(re.findall(r"inputs\.([A-Za-z0-9_]+)", rendered)):
        rendered = rendered.replace(f"inputs.{name}", repr((inputs or {}).get(name)))
    rendered = rendered.replace("always()", "True")
    rendered = rendered.replace("!cancelled()", repr(not cancelled))
    rendered = rendered.replace("&&", " and ").replace("||", " or ")
    rendered = _replace_outside_strings(rendered, r"\bfalse\b", "False")
    rendered = _replace_outside_strings(rendered, r"\btrue\b", "True")
    return bool(eval(rendered, {"__builtins__": {}}, {}))  # noqa: S307 - test-only DSL.


def _job_enabled(
    workflow: dict[str, Any],
    job_name: str,
    *,
    github: dict[str, Any],
    needs: dict[str, dict[str, Any]],
) -> bool:
    """Evaluate one job's conditional against simulated completed needs."""
    expression = workflow_job(workflow, job_name).get("if")
    if expression is None:
        return True
    return _expression_value(expression, github=github, needs=needs)


def _ready_jobs(
    workflow: dict[str, Any],
    *,
    github: dict[str, Any],
    completed: dict[str, dict[str, Any]],
) -> set[str]:
    """Return jobs that can start once their needs are completed and passing their if gate."""
    ready = set()
    for name, job in workflow["jobs"].items():
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        if all(need in completed for need in needs) and _job_enabled(
            workflow,
            name,
            github=github,
            needs=completed,
        ):
            ready.add(name)
    return ready


def _stable_public_tag_context() -> dict[str, Any]:
    """Return the release event shape for a stable public tag."""
    return {
        "event_name": "push",
        "ref_type": "tag",
        "private": False,
        "release_prerelease": None,
    }


def _successful_plan(**outputs: str) -> dict[str, Any]:
    """Return a simulated successful plan job with string outputs."""
    return {
        "result": "success",
        "outputs": {
            "candidate_run_id": "",
            "full_validation": "true",
            "is_prerelease": "false",
            **outputs,
        },
    }


def _successful_release(*, private: bool = False, prerelease: bool = False) -> dict[str, Any]:
    """Return simulated create-release outputs as Actions string values."""
    return {
        "result": "success",
        "outputs": {
            "is_private_repo": "true" if private else "false",
            "is_prerelease": "true" if prerelease else "false",
        },
    }


def _local_workflow_path(uses: str) -> Path | None:
    """Resolve a local reusable workflow reference."""
    prefix = "./"
    if not uses.startswith(prefix):
        return None
    path = ROOT / uses.removeprefix(prefix)
    if path.parent != ROOT / ".github" / "workflows":
        return None
    return path


def _assert_no_write_or_oidc_permission(node: dict[str, Any], label: str) -> None:
    """Reject write or OIDC permissions in a read-only reusable call chain."""
    permissions = node.get("permissions", {})
    if permissions in (None, {}, "read-all"):
        return
    if not isinstance(permissions, dict):
        raise AssertionError(f"{label} must use explicit read-only permissions")
    for scope, access in permissions.items():
        if scope == "id-token" or access == "write":
            raise AssertionError(f"{label} grants {scope}: {access}")


def _assert_reachable_reusable_jobs_are_read_only(
    workflows: dict[Path, dict[str, Any]],
    workflow_path: Path,
    job_name: str,
) -> None:
    """Recursively prove a read-only builder cannot reach write/OIDC jobs."""
    seen_workflows: set[Path] = set()

    def check_workflow(path: Path) -> None:
        if path in seen_workflows:
            return
        seen_workflows.add(path)
        workflow = workflows[path]
        _assert_no_write_or_oidc_permission(workflow, str(path))
        for name, job in workflow["jobs"].items():
            check_job(path, name, job)

    def check_job(path: Path, name: str, job: dict[str, Any]) -> None:
        label = f"{path.name}:{name}"
        _assert_no_write_or_oidc_permission(job, label)
        if target := _local_workflow_path(str(job.get("uses", ""))):
            check_workflow(target)

    check_job(workflow_path, job_name, workflows[workflow_path]["jobs"][job_name])


def test_read_only_docs_and_wheels_overlap_fresh_candidate_qualification() -> None:
    """Fresh stable tags can prebuild docs and wheels before native qualification is done."""
    workflow = _workflow(BUILD_RELEASE)
    completed = {"plan": _successful_plan()}
    ready = _ready_jobs(workflow, github=_stable_public_tag_context(), completed=completed)

    assert {
        "build-docs-artifact",
        "build-pypi-distributions",
        "candidate-checks",
        "platforms",
    } <= ready
    assert "candidate-ready" not in ready
    assert "verify-candidate" not in ready
    assert "create-release" not in ready
    assert workflow_job(workflow, "build-docs-artifact")["needs"] == ["plan"]
    assert workflow_job(workflow, "build-pypi-distributions")["needs"] == ["plan"]


def test_same_sha_candidate_promotion_keeps_prebuilds_but_not_publication_early() -> None:
    """Same-SHA promotion still prebuilds read-only artifacts while release waits for verify."""
    workflow = _workflow(BUILD_RELEASE)
    completed = {"plan": _successful_plan(candidate_run_id="123456")}
    ready = _ready_jobs(workflow, github=_stable_public_tag_context(), completed=completed)

    assert {"build-docs-artifact", "build-pypi-distributions"} <= ready
    assert "verify-candidate" not in ready
    assert "platforms" not in ready
    assert "candidate-checks" not in ready
    assert "candidate-ready" not in ready
    assert "create-release" not in ready

    completed.update(
        {
            "platforms": {"result": "skipped"},
            "candidate-checks": {"result": "skipped"},
            "candidate-ready": {"result": "skipped"},
        }
    )
    ready = _ready_jobs(workflow, github=_stable_public_tag_context(), completed=completed)
    assert "verify-candidate" in ready
    assert "create-release" not in ready


@pytest.mark.parametrize(
    ("completed", "job_name"),
    [
        (
            {"plan": {"result": "failure", "outputs": {"is_prerelease": "false"}}},
            "build-docs-artifact",
        ),
        (
            {"plan": {"result": "failure", "outputs": {"is_prerelease": "false"}}},
            "build-pypi-distributions",
        ),
        ({"plan": _successful_plan(is_prerelease="true")}, "build-docs-artifact"),
        ({"plan": _successful_plan(is_prerelease="true")}, "build-pypi-distributions"),
        ({"plan": _successful_plan()}, "create-release"),
    ],
)
def test_prebuild_and_publication_conditions_fail_closed(
    completed: dict[str, dict[str, Any]],
    job_name: str,
) -> None:
    """Failure, prerelease, and missing verification states do not pass release gates."""
    workflow = _workflow(BUILD_RELEASE)

    assert not _job_enabled(
        workflow,
        job_name,
        github=_stable_public_tag_context(),
        needs=completed,
    )


def test_private_stable_release_prebuilds_docs_but_skips_wheel_publication() -> None:
    """Private repositories keep docs behavior but never build or publish PyPI artifacts."""
    workflow = _workflow(BUILD_RELEASE)
    github = {"event_name": "push", "ref_type": "tag", "private": True}
    plan = {"plan": _successful_plan()}

    assert _job_enabled(workflow, "build-docs-artifact", github=github, needs=plan)
    assert not _job_enabled(workflow, "build-pypi-distributions", github=github, needs=plan)
    assert not _job_enabled(
        workflow,
        "publish-pypi",
        github=github,
        needs={
            "create-release": _successful_release(private=True),
            "build-pypi-distributions": {"result": "skipped"},
        },
    )


@pytest.mark.parametrize("create_release_result", ["failure", "cancelled", "skipped"])
def test_privileged_publishers_require_successful_github_release(
    create_release_result: str,
) -> None:
    """Docs and PyPI publication stay behind the verified GitHub release result."""
    workflow = _workflow(BUILD_RELEASE)
    needs = {
        "create-release": {"result": create_release_result, "outputs": {}},
        "build-docs-artifact": {"result": "success"},
        "build-pypi-distributions": {"result": "success"},
    }

    assert not _job_enabled(
        workflow, "deploy-docs", github=_stable_public_tag_context(), needs=needs
    )
    assert not _job_enabled(
        workflow, "publish-pypi", github=_stable_public_tag_context(), needs=needs
    )


@pytest.mark.parametrize(
    ("job_name", "failed_prebuild"),
    [
        ("deploy-docs", "build-docs-artifact"),
        ("publish-pypi", "build-pypi-distributions"),
    ],
)
def test_publication_requires_matching_successful_prebuild(
    job_name: str,
    failed_prebuild: str,
) -> None:
    """A failed read-only artifact build cannot be skipped over by a later publisher."""
    workflow = _workflow(BUILD_RELEASE)
    needs = {
        "create-release": _successful_release(),
        "build-docs-artifact": {"result": "success"},
        "build-pypi-distributions": {"result": "success"},
    }
    needs[failed_prebuild]["result"] = "failure"

    assert not _job_enabled(workflow, job_name, github=_stable_public_tag_context(), needs=needs)


def test_write_and_oidc_publication_jobs_execute_no_candidate_code() -> None:
    """Privileged publication jobs only download or deploy artifacts; they never checkout or run."""
    workflow = _workflow(BUILD_RELEASE)
    for job_name in ("create-release", "deploy-docs", "publish-pypi"):
        job = workflow_job(workflow, job_name)
        steps = job["steps"]
        assert all("run" not in step for step in steps)
        assert all("actions/checkout" not in step.get("uses", "") for step in steps)


def test_attempt_scoped_publication_artifact_names_and_compression() -> None:
    """Prebuilt publication artifacts are unique per attempt and skip redundant compression."""
    workflow = _workflow(BUILD_RELEASE)
    docs = workflow_job(workflow, "build-docs-artifact")
    pypi = workflow_job(workflow, "build-pypi-distributions")
    publisher = workflow_job(workflow, "publish-pypi")
    verifier_upload = next(
        step
        for step in workflow_job(workflow, "verify-candidate")["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
    )

    assert docs["uses"] == "./.github/workflows/docs-build.yml"
    assert docs["with"]["pages_artifact_name"] == DOCS_ARTIFACT
    assert pypi["with"]["artifact_name"] == PYPI_ARTIFACT
    assert (
        workflow_step(publisher, "Download Python distributions")["with"]["name"] == PYPI_ARTIFACT
    )
    assert (
        workflow_step(
            workflow_job(workflow, "create-release"), "Download verified release archives"
        )["with"]["name"]
        == VERIFIED_RELEASE_ARTIFACT
    )
    assert verifier_upload["with"]["name"] == VERIFIED_RELEASE_ARTIFACT
    assert verifier_upload["with"]["compression-level"] == 0


def test_read_only_docs_builder_reachable_jobs_have_no_write_or_oidc_permissions() -> None:
    """The build-release docs prebuild cannot statically reach Pages/OIDC jobs."""
    workflows = {
        BUILD_RELEASE: _workflow(BUILD_RELEASE),
        DOCS: _workflow(DOCS),
        DOCS_BUILD: _workflow(DOCS_BUILD),
    }

    _assert_reachable_reusable_jobs_are_read_only(
        workflows,
        BUILD_RELEASE,
        "build-docs-artifact",
    )


def test_read_only_docs_builder_permission_mutations_are_rejected() -> None:
    """Recursive permission guard fails if a called build workflow gains publish power."""
    workflows = {
        BUILD_RELEASE: _workflow(BUILD_RELEASE),
        DOCS: _workflow(DOCS),
        DOCS_BUILD: _workflow(DOCS_BUILD),
    }
    workflows[DOCS_BUILD]["jobs"]["build"]["permissions"]["id-token"] = "write"

    with pytest.raises(AssertionError):
        _assert_reachable_reusable_jobs_are_read_only(
            workflows,
            BUILD_RELEASE,
            "build-docs-artifact",
        )


def test_pypi_distribution_workflow_is_reusable_read_only_and_compression_free() -> None:
    """The wheel builder exposes build-only timing inputs without publication privileges."""
    workflow = _workflow(PYPI_DISTRIBUTIONS)
    build = workflow_job(workflow, "build")
    upload = workflow_step(build, "Upload Python distributions")

    assert "workflow_call" in workflow["on"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    assert build["permissions"] == {"contents": "read"}
    assert workflow_step(build, "Install locked build dependencies")["run"] == (
        "uv sync --frozen --extra dev"
    )
    assert workflow_step(build, "Build Python package")["run"] == (
        "uv build --no-build-isolation --no-sources"
    )
    assert upload["if"] == "inputs.upload_artifact != false"
    assert upload["with"]["name"] == "${{ inputs.artifact_name || 'pypi-distributions' }}"
    assert upload["with"]["compression-level"] == 0


def test_docs_workflow_preserves_pr_build_and_release_only_publication() -> None:
    """PR docs builds stay build-only; release and reusable callers control upload/deploy."""
    workflow = _workflow(DOCS)
    build = workflow_job(workflow, "build")
    deploy = workflow_job(workflow, "deploy")
    pull_request = {
        "event_name": "pull_request",
        "ref_type": "branch",
        "private": False,
        "release_prerelease": None,
    }
    release = {
        "event_name": "release",
        "ref_type": "tag",
        "private": False,
        "release_prerelease": False,
    }
    manual = {
        "event_name": "workflow_dispatch",
        "ref_type": "branch",
        "private": False,
        "release_prerelease": None,
    }

    assert build["uses"] == "./.github/workflows/docs-build.yml"
    assert build["permissions"] == {"contents": "read"}
    assert build["with"]["pages_artifact_name"] == (
        "${{ inputs.pages_artifact_name || 'github-pages' }}"
    )
    assert not _expression_value(
        build["with"]["upload_pages_artifact"],
        github=pull_request,
        needs={},
        inputs={},
    )
    assert not _expression_value(
        deploy["if"], github=pull_request, needs={"build": {"result": "success"}}
    )
    assert _expression_value(
        build["with"]["upload_pages_artifact"], github=release, needs={}, inputs={}
    )
    assert _expression_value(deploy["if"], github=release, needs={"build": {"result": "success"}})
    assert _expression_value(
        build["with"]["upload_pages_artifact"],
        github=manual,
        needs={},
        inputs={"upload_pages_artifact": True},
    )
    assert not _expression_value(
        deploy["if"],
        github=manual,
        needs={"build": {"result": "success"}},
        inputs={"deploy": False},
    )
    assert deploy["permissions"] == {"pages": "write", "id-token": "write"}


def test_docs_build_workflow_exposes_read_only_upload_interface() -> None:
    """The canonical docs build can be timed and uploaded without publish permissions."""
    workflow = _workflow(DOCS_BUILD)
    build = workflow_job(workflow, "build")
    upload = workflow_step(build, "Upload build artifacts")

    assert "workflow_call" in workflow["on"]
    assert "workflow_dispatch" in workflow["on"]
    assert workflow["permissions"] == {"contents": "read"}
    assert build["permissions"] == {"contents": "read"}
    assert upload["if"] == "inputs.upload_pages_artifact != false"
    assert upload["with"] == {
        "name": "${{ inputs.pages_artifact_name || 'github-pages' }}",
        "path": "docs/dist",
    }
