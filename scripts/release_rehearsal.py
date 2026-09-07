"""Compare a complete read-only release rehearsal without adding parallel job savings."""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from scripts.compare_test_runs import Report, compare, read_report
from scripts.package_release import BINARY_NAMES, require_sha
from scripts.performance_cohort import MEMBERS, verify

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Surface:
    """One predeclared paired surface; ordinary native obligations remain unchanged."""

    key: str
    binary: str
    suite: str
    cpus: str
    selection: str


SURFACES = (
    Surface(
        "arm-integration", "apm-darwin-arm64", "integration", "3", "tests/integration/ -m not live"
    ),
    Surface(
        "linux-integration",
        "apm-linux-x86_64",
        "integration",
        "4",
        "tests/integration/ -m not live",
    ),
    Surface("intel-units", "apm-darwin-x86_64", "unit", "4", "tests/unit tests/test_console.py"),
)


def rehearsal_matrix(catalog: list[dict]) -> dict[str, list[dict]]:
    """Add opt-in comparison inputs without changing the canonical baseline catalog."""
    if len(catalog) != 5 or {row["binary_name"] for row in catalog} != set(BINARY_NAMES):
        raise ValueError("A rehearsal must retain all five unique native platforms")
    rows = []
    for row in catalog:
        if (
            row["integration_shard_count"] != 1
            or row["integration_xdist_workers"] != 4
            or row["integration_splitting_algorithm"] != "duration_based_chunks"
        ):
            raise ValueError("The predeclared rehearsal baseline is one four-worker native shard")
        binary = row["binary_name"]
        rows.append(
            {
                **row,
                "unit_performance_probe": binary == "apm-darwin-x86_64",
                "integration_performance_probe": binary in {"apm-darwin-arm64", "apm-linux-x86_64"},
                "performance_workers": 3 if binary == "apm-darwin-arm64" else 4,
            }
        )
    return {"include": rows}


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Explicit timezone-aware job timestamps are required")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Job timestamps must include a timezone")
    return result


def _job_times(job: dict, origin: datetime) -> tuple[datetime, datetime]:
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        raise ValueError(f"Required job did not succeed: {job.get('name')}")
    start, end = _timestamp(job.get("started_at")), _timestamp(job.get("completed_at"))
    if start < origin or end < start:
        raise ValueError(f"Invalid required job timeline: {job.get('name')}")
    return start, end


def _one(jobs: list[dict], name: str, origin: datetime, prefix: str | None = None) -> dict:
    matches = [
        job
        for job in jobs
        if job.get("name") == name
        or (
            prefix is not None
            and isinstance(job.get("name"), str)
            and job["name"].startswith(prefix + " / ")
            and job["name"].endswith(" / " + name)
        )
    ]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one required job: {prefix or ''} / {name}")
    _job_times(matches[0], origin)
    return matches[0]


def source_authorities() -> list[str]:
    """Ask the existing release authority for its exact source checks, never fork the list."""
    node = shutil.which("node")
    if node is None:
        raise OSError("Node.js is required to read the canonical release authorities")
    result = subprocess.run(  # noqa: S603 - installed Node and a constant repository authority module.
        [
            node,
            "-e",
            "process.stdout.write(JSON.stringify(require('./scripts/release-candidate.cjs').REQUIRED_SOURCE_JOB_NAMES))",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    names = json.loads(result.stdout)
    if not isinstance(names, list) or not names or any(not isinstance(name, str) for name in names):
        raise ValueError("Canonical source authorities must be a nonempty name list")
    return names


def _common_jobs(jobs: list[dict], origin: datetime, source_names: list[str]) -> list[dict]:
    common = [_one(jobs, name, origin) for name in source_names]
    for binary in BINARY_NAMES:
        _one(jobs, f"{binary} / Native Candidate Gate", origin)
        common.append(_one(jobs, "Build Candidate", origin, binary))
        common.append(_one(jobs, "Isolated Release Validation", origin, binary))
        if binary != "apm-darwin-x86_64":
            common.append(_one(jobs, "Unit Tests Baseline", origin, binary))
        if binary not in {"apm-darwin-arm64", "apm-linux-x86_64"}:
            common.append(_one(jobs, "Integration Tests Shard 1", origin, binary))
        if binary == "apm-windows-x86_64":
            common.append(_one(jobs, "Candidate Windows Installer", origin, binary))
    common.extend(
        [
            _one(jobs, "Read-only documentation build / build", origin),
            _one(jobs, "Read-only Python distribution build / Build PyPI Distributions", origin),
        ]
    )
    if len({job["id"] for job in common}) != len(common):
        raise ValueError("A required job was counted twice")
    return common


def _bind_member(
    root: Path, member: str, jobs: list[dict], surface: Surface, origin: datetime
) -> dict:
    data = json.loads((root / f"{member}.json").read_text("ascii"))
    trace = data["execution"]
    runner = trace["runner_name"]
    recorded = _timestamp(trace["recorded_at"])
    if not isinstance(runner, str) or not runner:
        raise ValueError("Cohort must identify its actual GitHub runner")
    matches = []
    for job in jobs:
        if (
            job.get("runner_name") == runner
            and isinstance(job.get("name"), str)
            and job["name"].startswith(surface.binary + " / ")
            and job.get("status") == "completed"
        ):
            start, end = _job_times(job, origin)
            if start <= recorded <= end:
                matches.append(job)
    if len(matches) != 1:
        raise ValueError(f"Cannot uniquely bind {surface.key}/{member} to its actual native job")
    return matches[0]


def _surface_for(report: Report) -> Surface:
    candidate = report.identity["candidate"]
    for surface in SURFACES:
        if report.identity["selection"] == surface.selection and (
            (candidate is None and surface.suite == "unit")
            or (isinstance(candidate, dict) and candidate.get("binary_name") == surface.binary)
        ):
            return surface
    raise ValueError("Unexpected performance evidence outside the predeclared three surfaces")


def _native_authorities(
    jobs: list[dict], pairs: dict, origin: datetime
) -> tuple[list[dict], list[dict]]:
    """Retain native gates, propagating their observed dependency-to-verdict delays."""
    authorities, gate_jobs = [], []

    def end(parts: list[dict]) -> float:
        return max((_job_times(job, origin)[1] - origin).total_seconds() for job in parts)

    def delay(gate: dict, prerequisites: list[dict]) -> float:
        value = end([gate]) - end(prerequisites)
        if value < 0:
            raise ValueError(f"Required gate completed before its prerequisites: {gate['name']}")
        return value

    for binary in sorted(BINARY_NAMES):
        surfaces = {surface.suite: surface.key for surface in SURFACES if surface.binary == binary}
        unit_key, integration_key = surfaces.get("unit"), surfaces.get("integration")
        unit = (
            pairs[unit_key]
            if unit_key
            else {
                "baseline": [_one(jobs, "Unit Tests Baseline", origin, binary)],
            }
        )
        integration = (
            pairs[integration_key]
            if integration_key
            else {
                "baseline": [_one(jobs, "Integration Tests Shard 1", origin, binary)],
            }
        )
        integration_gate = _one(jobs, "Integration Tests", origin, binary)
        integration_delay = delay(integration_gate, integration["baseline"])
        observed_unit = unit["baseline"]
        if unit_key:
            # This extra checker exists only to join both experimental unit worlds.
            unit_probe = _one(jobs, "Unit Performance Probe", origin, binary)
            delay(unit_probe, [*unit["baseline"], *unit["proposed"]])
            observed_unit = [unit_probe]
        independent = [
            _one(jobs, "Build Candidate", origin, binary),
            _one(jobs, "Isolated Release Validation", origin, binary),
        ]
        if binary == "apm-windows-x86_64":
            independent.append(_one(jobs, "Candidate Windows Installer", origin, binary))
        gate = _one(jobs, f"{binary} / Native Candidate Gate", origin)
        native_delay = delay(gate, [*independent, *observed_unit, integration_gate])
        gate_jobs.extend([integration_gate, gate])
        authorities.append(
            {
                "name": gate["name"],
                "observed_completed_at": gate["completed_at"],
                "independent_ready_seconds": end(independent),
                "unit_surface": unit_key,
                "integration_surface": integration_key,
                "unit_ready_seconds": {variant: end(parts) for variant, parts in unit.items()},
                "integration_ready_seconds": {
                    variant: end(parts) + integration_delay
                    for variant, parts in integration.items()
                },
                "integration_fan_in_delay_seconds": integration_delay,
                "native_gate_delay_seconds": native_delay,
            }
        )
    return authorities, gate_jobs


def _scenario(
    common: list[dict],
    selected: list[dict],
    origin: datetime,
    authorities: list[dict],
    gate_jobs: list[dict],
    enabled: set[str],
) -> dict:
    jobs = [*common, *selected]
    end_job = max(jobs, key=lambda job: _job_times(job, origin)[1])
    ready = (_job_times(end_job, origin)[1] - origin).total_seconds()
    limiting = end_job["name"]
    native = []
    for authority in authorities:
        unit_variant = "proposed" if authority["unit_surface"] in enabled else "baseline"
        integration_variant = (
            "proposed" if authority["integration_surface"] in enabled else "baseline"
        )
        completed = (
            max(
                authority["independent_ready_seconds"],
                authority["unit_ready_seconds"][unit_variant],
                authority["integration_ready_seconds"][integration_variant],
            )
            + authority["native_gate_delay_seconds"]
        )
        native.append({**authority, "modeled_ready_seconds": completed})
        if completed >= ready:
            ready, limiting = completed, authority["name"]
    return {
        "evidence_ready_seconds": ready,
        "limiting_job": limiting,
        "native_authorities": native,
        "selected_runner_seconds": sum(
            (end - start).total_seconds()
            for start, end in (_job_times(job, origin) for job in [*jobs, *gate_jobs])
        ),
    }


def evaluate(
    metadata: dict,
    reports: list[Report],
    cohorts: Path,
    expected_sha: str,
    source_names: list[str],
    minimum_lane_gain: float,
    minimum_overall_gain: float,
) -> dict:
    """Reconstruct complete evidence-ready paths, preserving all common and paired work."""
    require_sha(expected_sha)
    for threshold in (minimum_lane_gain, minimum_overall_gain):
        if not math.isfinite(threshold) or not 0 <= threshold < 1:
            raise ValueError("Performance thresholds must be finite fractions in [0, 1)")
    if (
        type(metadata.get("schema_version")) is not int
        or metadata["schema_version"] != 1
        or metadata.get("source_sha") != expected_sha
        or type(metadata.get("run_attempt")) is not int
        or metadata["run_attempt"] < 1
    ):
        raise ValueError("Current exact-SHA workflow attempt metadata is required")
    run_id, attempt = metadata["run_id"], metadata["run_attempt"]
    origin = _timestamp(metadata["created_at"] if attempt == 1 else metadata["run_started_at"])
    jobs = metadata["jobs"]
    if not isinstance(jobs, list) or not jobs or len({job["id"] for job in jobs}) != len(jobs):
        raise ValueError("A complete unique current-attempt job inventory is required")
    if any(job.get("run_id") != int(run_id) for job in jobs):
        raise ValueError("A job belongs to another workflow run")
    if any(job.get("run_attempt") != attempt for job in jobs):
        raise ValueError("A job belongs to another workflow attempt")
    common = _common_jobs(jobs, origin, source_names)
    grouped = {surface.key: [] for surface in SURFACES}
    for report in reports:
        grouped[_surface_for(report).key].append(report)
    pairs, comparisons = {}, {}
    bound_ids = set()
    for surface in SURFACES:
        subset = grouped[surface.key]
        baseline = [report for report in subset if report.variant == "baseline"]
        proposed = [report for report in subset if report.variant == "proposed"]
        if len(baseline) != 1 or baseline[0].shard_count != 1 or len(proposed) != 2:
            raise ValueError(f"Expected the exact one-versus-two experiment for {surface.key}")
        result = compare(baseline, proposed, expected_sha, minimum_lane_gain)
        if result["identity"]["environment"]["cpu_count"] != surface.cpus:
            raise ValueError(f"Unexpected runner capacity for {surface.key}")
        root = cohorts / surface.key
        before = verify(
            root, f"{surface.suite}-{attempt}-{surface.binary}", run_id, attempt, expected_sha
        )
        if before != result["identity"]:
            raise ValueError(
                f"Identity drifted between preflight and completed tests: {surface.key}"
            )
        bound = [_bind_member(root, member, jobs, surface, origin) for member in MEMBERS]
        for member, job in zip(MEMBERS, bound, strict=True):
            report = next(item for item in subset if f"{item.variant}-{item.shard}" == member)
            start, end = _job_times(job, origin)
            if report.elapsed_seconds > (end - start).total_seconds() + 2:
                raise ValueError(
                    f"Pytest timing exceeds its actual job span: {surface.key}/{member}"
                )
        if bound_ids & {job["id"] for job in bound} or len({job["id"] for job in bound}) != 3:
            raise ValueError("Paired partitions must identify distinct actual execution jobs")
        bound_ids.update(job["id"] for job in bound)
        pairs[surface.key] = {"baseline": bound[:1], "proposed": bound[1:]}
        comparisons[surface.key] = {
            **result,
            "actual_jobs": [
                {
                    "id": job["id"],
                    "name": job["name"],
                    "runner_name": job["runner_name"],
                    "started_at": job["started_at"],
                    "completed_at": job["completed_at"],
                    "cohort_steps": [
                        step
                        for step in job.get("steps", [])
                        if "cohort" in step.get("name", "").lower()
                        or "matched actual" in step.get("name", "").lower()
                    ],
                }
                for job in bound
            ],
        }
    if bound_ids & {job["id"] for job in common}:
        raise ValueError("A paired execution was also counted as unchanged work")
    authorities, gate_jobs = _native_authorities(jobs, pairs, origin)
    scenarios = {}
    proposals = {
        "baseline": set(),
        "arm_only": {"arm-integration"},
        "arm_and_linux": {"arm-integration", "linux-integration"},
        "combined": set(pairs),
    }
    for name, enabled in proposals.items():
        selected = [
            job
            for key, pair in pairs.items()
            for job in pair["proposed" if key in enabled else "baseline"]
        ]
        scenarios[name] = _scenario(common, selected, origin, authorities, gate_jobs, enabled)
    before = scenarios["baseline"]["evidence_ready_seconds"]
    after = scenarios["combined"]["evidence_ready_seconds"]
    if before <= 0:
        raise ValueError("Baseline must contain positive observed elapsed time")
    gain = 1 - after / before
    meets_lanes = all(result["meets_threshold"] for result in comparisons.values())
    return {
        "schema_version": 1,
        "run_id": run_id,
        "run_attempt": attempt,
        "source_sha": expected_sha,
        "scope": "Job-timestamp reconstruction with observed native fan-in delays propagated "
        "through each proposed DAG; "
        "not actual production release or the all-variants rehearsal workflow duration",
        "origin": origin.isoformat(),
        "origin_kind": "workflow creation" if attempt == 1 else "attempt start",
        "common_job_count": len(common),
        "common_jobs": [job["name"] for job in common],
        "comparisons": comparisons,
        "scenarios": scenarios,
        "saved_seconds": before - after,
        "overall_gain_fraction": gain,
        "minimum_overall_gain": minimum_overall_gain,
        "meets_lane_thresholds": meets_lanes,
        "meets_minimum": meets_lanes and gain >= minimum_overall_gain,
        "reaches_30_percent_target": meets_lanes and gain >= 0.30,
        "unchanged_tail_sensitivity": [
            {"tail_seconds": tail, "gain_fraction": (before - after) / (before + tail)}
            for tail in (0, 60, 120, 180)
        ],
        "limitations": [
            "Secret-bearing acceptance and Windows signing are not represented.",
            "Native integration/native-gate delays are observed then held constant across modeled worlds; "
            "their proposed completion timestamps are not independently measured.",
            "Only experiment-specific both-world fan-ins are excluded; production native gates are retained.",
            "Recording qualification, verifying publication assets and publishing are excluded.",
            "Both worlds share one run; extra experimental concurrency and cohort waits can affect allocation.",
            "JUnit case durations include fixtures and waiting, not fixed CPU work.",
        ],
    }


def main() -> int:
    """Emit a matrix or durable strict rehearsal result; missed targets remain visible."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    commands.add_parser("matrix")
    check = commands.add_parser("compare")
    check.add_argument("--jobs", type=Path, required=True)
    check.add_argument("--reports", type=Path, required=True)
    check.add_argument("--cohorts", type=Path, required=True)
    check.add_argument("--expected-sha", required=True)
    check.add_argument("--minimum-lane-gain", type=float, default=0.15)
    check.add_argument("--minimum-overall-gain", type=float, default=0.10)
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "matrix":
            catalog = json.loads((ROOT / "scripts/release-platforms.json").read_text("ascii"))
            print("matrix=" + json.dumps(rehearsal_matrix(catalog), separators=(",", ":")))
            return 0
        result = evaluate(
            json.loads(args.jobs.read_text("utf-8")),
            [read_report(path) for path in sorted(args.reports.rglob("*.json"))],
            args.cohorts,
            args.expected_sha,
            source_authorities(),
            args.minimum_lane_gain,
            args.minimum_overall_gain,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", "ascii")
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError) as error:
        parser.error(f"Invalid release rehearsal: {error}")
    if not result["meets_minimum"]:
        parser.exit(1, f"Rehearsal target missed; inspect {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
