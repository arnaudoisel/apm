"""Fail before expensive tests unless the actual paired runners have equal identities."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from scripts.compare_test_runs import capture_identity, validate_identity
from scripts.package_release import require_sha

MEMBERS = ("baseline-1", "proposed-1", "proposed-2")


def require_context(cohort: str, run_id: str, attempt: int) -> None:
    """Restrict artifact names and bind them to one complete workflow attempt."""
    if not isinstance(run_id, str) or re.fullmatch(r"[1-9][0-9]*", run_id) is None:
        raise ValueError("A positive workflow run ID is required")
    if type(attempt) is not int or attempt < 1:
        raise ValueError("A positive workflow attempt is required")
    if (
        not isinstance(cohort, str)
        or re.fullmatch(rf"(unit|integration)-{attempt}-apm-[a-z0-9]+-[a-z0-9_]+", cohort) is None
    ):
        raise ValueError("Cohort must identify a suite, current attempt and native binary")


def record(
    cohort: str,
    member: str,
    run_id: str,
    attempt: int,
    source_sha: str,
    selection: str,
    candidate_metadata: Path | None = None,
) -> dict[str, object]:
    """Record identity on the runner that will execute this exact test partition."""
    require_context(cohort, run_id, attempt)
    if member not in MEMBERS:
        raise ValueError("Unknown cohort member")
    identity = capture_identity(source_sha, selection, candidate_metadata)
    if not identity["environment"]["runner_image"]:
        raise ValueError("Hosted runner ImageVersion is required for a controlled cohort")
    if cohort.startswith("integration-") != (identity["candidate"] is not None):
        raise ValueError("Only integration cohorts require candidate archive metadata")
    runner_name = os.environ.get("RUNNER_NAME", "")
    workflow_job = os.environ.get("GITHUB_JOB", "")
    if not runner_name or not workflow_job:
        raise ValueError("Actual GitHub runner and workflow job identity are required")
    return {
        "schema_version": 1,
        "cohort": cohort,
        "member": member,
        "run_id": run_id,
        "run_attempt": attempt,
        "identity": identity,
        "execution": {
            "runner_name": runner_name,
            "workflow_job": workflow_job,
            "recorded_at": datetime.now(UTC).isoformat(),
        },
    }


def verify(
    root: Path, cohort: str, run_id: str, attempt: int, source_sha: str
) -> dict[str, object]:
    """Reject missing, duplicate, stale, malformed or environmentally different members."""
    require_context(cohort, run_id, attempt)
    require_sha(source_sha)
    if {path.name for path in root.iterdir()} != {f"{member}.json" for member in MEMBERS}:
        raise ValueError("Exactly the three declared cohort member files are required")
    identities = []
    for member in MEMBERS:
        data = json.loads((root / f"{member}.json").read_text("ascii"))
        if (
            not isinstance(data, dict)
            or type(data.get("schema_version")) is not int
            or data["schema_version"] != 1
            or data.get("cohort") != cohort
            or data.get("member") != member
            or data.get("run_id") != run_id
            or type(data.get("run_attempt")) is not int
            or data["run_attempt"] != attempt
            or not isinstance(data.get("identity"), dict)
        ):
            raise ValueError(f"Invalid or stale cohort evidence for {member}")
        identity = validate_identity(data["identity"])
        if identity["source_sha"] != source_sha or not identity["environment"]["runner_image"]:
            raise ValueError("Cohort source and hosted image must be explicit")
        if cohort.startswith("integration-") != (identity["candidate"] is not None):
            raise ValueError("Cohort suite does not match its candidate evidence")
        execution = data.get("execution")
        if (
            not isinstance(execution, dict)
            or any(
                not isinstance(execution.get(key), str) or not execution[key]
                for key in ("runner_name", "workflow_job", "recorded_at")
            )
            or datetime.fromisoformat(execution["recorded_at"]).tzinfo is None
        ):
            raise ValueError("Cohort must bind its actual runner and timestamp to the job timeline")
        identities.append(identity)
    if any(identity != identities[0] for identity in identities[1:]):
        raise ValueError("Cohort identity mismatch; do not run or claim comparable performance")
    return identities[0]


def main() -> int:
    """Capture or validate a pre-execution cohort without producing a pass substitute."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    capture = commands.add_parser("record")
    capture.add_argument("--member", choices=MEMBERS, required=True)
    capture.add_argument("--selection", required=True)
    capture.add_argument("--candidate-metadata", type=Path)
    capture.add_argument("--output", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--root", type=Path, required=True)
    for command in (capture, check):
        command.add_argument("--cohort", required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--attempt", type=int, required=True)
        command.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    try:
        if args.operation == "record":
            data = record(
                args.cohort,
                args.member,
                args.run_id,
                args.attempt,
                args.source_sha,
                args.selection,
                args.candidate_metadata,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", "ascii")
        else:
            verify(args.root, args.cohort, args.run_id, args.attempt, args.source_sha)
    except (ValueError, KeyError, TypeError, OSError) as error:
        parser.error(f"Invalid performance cohort: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
