"""Preflight matching must not let a mismatched runner spend time on the full suite."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import performance_cohort

pytestmark = pytest.mark.component
SHA = "a" * 40
COHORT = "unit-2-apm-darwin-x86_64"
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def identity(monkeypatch: pytest.MonkeyPatch) -> dict:
    value = {
        "source_sha": SHA,
        "selection": "tests/unit tests/test_console.py",
        "candidate": None,
        "environment": {
            "python": "3.12.10",
            "system": "Darwin",
            "release": "24.6.0",
            "machine": "x86_64",
            "cpu_count": "4",
            "runner_image": "20260831.1",
            "dependency_lock": "f" * 64,
        },
    }
    monkeypatch.setenv("RUNNER_NAME", "GitHub Actions test")
    monkeypatch.setenv("GITHUB_JOB", "unit-tests")
    monkeypatch.setattr(performance_cohort, "capture_identity", lambda *args: value)
    return value


def _members(root: Path) -> None:
    for member in performance_cohort.MEMBERS:
        data = performance_cohort.record(
            COHORT,
            member,
            "123",
            2,
            SHA,
            "tests/unit tests/test_console.py",
        )
        (root / f"{member}.json").write_text(json.dumps(data), encoding="ascii")


def test_matching_actual_runners_produce_the_identity_used_by_final_proof(
    tmp_path: Path,
    identity: dict,
) -> None:
    _members(tmp_path)
    assert performance_cohort.verify(tmp_path, COHORT, "123", 2, SHA) == identity


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "extra",
        "member",
        "run",
        "attempt",
        "schema",
        "sha",
        "image",
        "empty-image",
        "lock",
        "selection",
        "candidate",
        "malformed",
        "missing-runner",
        "missing-job",
        "invalid-time",
        "naive-time",
    ],
)
def test_preflight_rejects_incomplete_stale_or_incomparable_cohorts(
    tmp_path: Path,
    identity: dict,
    fault: str,
) -> None:
    _members(tmp_path)
    path = tmp_path / "proposed-2.json"
    data = json.loads(path.read_text("ascii"))
    if fault == "missing":
        path.unlink()
    elif fault == "extra":
        (tmp_path / "duplicate.json").write_text(json.dumps(data), encoding="ascii")
    elif fault == "malformed":
        path.write_text("not json", encoding="ascii")
    else:
        if fault == "member":
            data["member"] = "baseline-1"
        elif fault == "run":
            data["run_id"] = "122"
        elif fault == "attempt":
            data["run_attempt"] = 1
        elif fault == "schema":
            data["schema_version"] = True
        elif fault == "sha":
            data["identity"]["source_sha"] = "b" * 40
        elif fault == "image":
            data["identity"]["environment"]["runner_image"] = "different-image"
        elif fault == "empty-image":
            data["identity"]["environment"]["runner_image"] = ""
        elif fault == "lock":
            data["identity"]["environment"]["dependency_lock"] = "b" * 64
        elif fault == "selection":
            data["identity"]["selection"] = "fewer tests"
        elif fault == "missing-runner":
            data["execution"]["runner_name"] = ""
        elif fault == "missing-job":
            data["execution"].pop("workflow_job")
        elif fault == "invalid-time":
            data["execution"]["recorded_at"] = "not a timestamp"
        elif fault == "naive-time":
            data["execution"]["recorded_at"] = "2026-09-07T12:00:00"
        else:
            data["identity"]["candidate"] = {"schema_version": 0}
        path.write_text(json.dumps(data), encoding="ascii")
    with pytest.raises((ValueError, KeyError, TypeError)):
        performance_cohort.verify(tmp_path, COHORT, "123", 2, SHA)


@pytest.mark.parametrize(
    ("cohort", "run_id", "attempt"),
    [(COHORT, "123", 1), ("../../escape", "123", 2), (COHORT, "0", 2), (COHORT, "123", True)],
)
def test_rejects_unbounded_or_wrong_attempt_context(cohort: str, run_id: str, attempt: int) -> None:
    with pytest.raises(ValueError):
        performance_cohort.require_context(cohort, run_id, attempt)


def test_capture_rejects_missing_hosted_image(identity: dict) -> None:
    identity["environment"]["runner_image"] = ""
    with pytest.raises(ValueError, match="ImageVersion"):
        performance_cohort.record(COHORT, "baseline-1", "123", 2, SHA, "unit")


def test_capture_rejects_unknown_member(identity: dict) -> None:
    with pytest.raises(ValueError, match="Unknown"):
        performance_cohort.record(COHORT, "proposed-3", "123", 2, SHA, "unit")


@pytest.mark.parametrize("variable", ["RUNNER_NAME", "GITHUB_JOB"])
def test_capture_cannot_emit_unbindable_runner_evidence(
    identity: dict,
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
) -> None:
    monkeypatch.delenv(variable)
    with pytest.raises(ValueError, match="runner and workflow job identity"):
        performance_cohort.record(COHORT, "baseline-1", "123", 2, SHA, "unit")


def test_integration_cohort_cannot_omit_the_candidate(identity: dict) -> None:
    with pytest.raises(ValueError, match="candidate archive metadata"):
        performance_cohort.record(
            "integration-2-apm-darwin-arm64", "baseline-1", "123", 2, SHA, "integration"
        )


def test_actual_node_artifact_waiter_handles_failures_without_network() -> None:
    result = subprocess.run(
        ["node", "--test", str(ROOT / "scripts/performance-cohort.test.cjs")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
