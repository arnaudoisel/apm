"""Compare pytest execution time only after proving equivalent test evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from scripts.package_release import archive_name, file_digest, require_sha

OUTCOMES = {"passed", "skipped", "xfailed", "failed", "error"}


class _JUnitTreeBuilder(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        """Reject DTDs before the XML parser can expand custom entities."""
        raise ValueError("JUnit document type declarations are forbidden")


@dataclass(frozen=True)
class Report:
    """One pytest invocation, including its explicit source and shard identity."""

    identity: dict[str, object]
    variant: str
    shard: int
    shard_count: int
    elapsed_seconds: float
    cases: dict[tuple[str, str], str]

    def payload(self) -> dict[str, object]:
        """Return a deterministic, inspectable evidence document."""
        return {
            "schema_version": 1,
            "identity": self.identity,
            "variant": self.variant,
            "shard": self.shard,
            "shard_count": self.shard_count,
            "elapsed_seconds": self.elapsed_seconds,
            "cases": [
                {"id": list(node), "outcome": outcome}
                for node, outcome in sorted(self.cases.items())
            ],
        }


def _environment() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_count": str(os.cpu_count()),
        "runner_image": os.environ.get("ImageVersion", ""),  # noqa: SIM112 - runner-images contract.
        "dependency_lock": file_digest(Path(__file__).resolve().parents[1] / "uv.lock"),
    }


def _identity(
    source_sha: str, selection: str, candidate: object, environment: object
) -> dict[str, object]:
    require_sha(source_sha)
    if not isinstance(selection, str) or not selection.strip():
        raise ValueError("A nonempty test selection is required")
    if (
        not isinstance(environment, dict)
        or set(environment)
        != {
            "python",
            "system",
            "release",
            "machine",
            "cpu_count",
            "runner_image",
            "dependency_lock",
        }
        or not all(isinstance(value, str) for value in environment.values())
        or any(not value for key, value in environment.items() if key != "runner_image")
    ):
        raise ValueError("Explicit runtime/runner environment evidence is required")
    if candidate is not None:
        if not isinstance(candidate, dict) or type(candidate.get("schema_version")) is not int:
            raise ValueError("Invalid candidate metadata schema")
        if candidate["schema_version"] != 1 or candidate.get("sha") != source_sha:
            raise ValueError("Candidate metadata does not identify the expected source")
        binary_name = candidate.get("binary_name")
        if not isinstance(binary_name, str) or candidate.get("archive") != archive_name(
            binary_name
        ):
            raise ValueError("Candidate metadata has an invalid archive identity")
        if not isinstance(candidate.get("version"), str) or not candidate["version"]:
            raise ValueError("Candidate metadata has no version")
        for field in ("archive_sha256", "executable_sha256"):
            value = candidate.get(field)
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError(f"Invalid candidate {field}")
    return {
        "source_sha": source_sha,
        "selection": selection,
        "candidate": candidate,
        "environment": environment,
    }


def _positive_seconds(value: object) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError("Elapsed time must be finite and positive")
    return float(value)


def _shard_identity(variant: str, shard: int, count: int) -> None:
    if variant not in ("baseline", "proposed"):
        raise ValueError("Variant must be baseline or proposed")
    if type(shard) is not int or type(count) is not int or not 1 <= shard <= count <= 16:
        raise ValueError("Require a complete bounded shard identity: 1 <= shard <= count <= 16")


def capture(
    junit: Path,
    source_sha: str,
    selection: str,
    variant: str,
    shard: int,
    shard_count: int,
    candidate_metadata: Path | None = None,
) -> Report:
    """Read one real pytest JUnit report without equating skips with passes."""
    _shard_identity(variant, shard, shard_count)
    candidate = json.loads(candidate_metadata.read_text("utf-8")) if candidate_metadata else None
    identity = _identity(source_sha, selection, candidate, _environment())
    parser = ET.XMLParser(target=_JUnitTreeBuilder())  # noqa: S314 - rejects all DTD declarations.
    root = ET.parse(junit, parser=parser).getroot()  # noqa: S314 - DTDs rejected before expansion.
    suites = [root] if root.tag == "testsuite" else list(root)
    if len(suites) != 1 or suites[0].tag != "testsuite":
        raise ValueError("Expected the single-suite JUnit output of one pytest invocation")
    suite = suites[0]
    elapsed = _positive_seconds(float(suite.attrib["time"]))
    cases = {}
    for node in suite.findall("testcase"):
        identities = node.findall("./properties/property[@name='apm_performance_nodeid']")
        if len(identities) > 1:
            raise ValueError("Duplicate original node identity in JUnit")
        key = (
            ("pytest-nodeid", identities[0].attrib["value"])
            if identities
            else (node.attrib.get("classname", ""), node.attrib["name"])
        )
        if not key[1] or key in cases:
            raise ValueError(f"Empty or duplicate JUnit case identity: {key!r}")
        failures = [child for child in node if child.tag in ("failure", "error", "skipped")]
        if len(failures) > 1:
            raise ValueError(f"Ambiguous JUnit outcome: {key!r}")
        if not failures:
            outcome = "passed"
        elif failures[0].tag == "skipped":
            outcome = "xfailed" if failures[0].get("type") == "pytest.xfail" else "skipped"
        else:
            outcome = "failed" if failures[0].tag == "failure" else "error"
        cases[key] = outcome
    counts = {
        "tests": len(cases),
        "failures": sum(value == "failed" for value in cases.values()),
        "errors": sum(value == "error" for value in cases.values()),
        "skipped": sum(value in ("skipped", "xfailed") for value in cases.values()),
    }
    if not cases or any(int(suite.attrib[field]) != count for field, count in counts.items()):
        raise ValueError("JUnit summary counts do not match the nonempty case inventory")
    return Report(identity, variant, shard, shard_count, elapsed, cases)


def read_report(path: Path) -> Report:
    """Reject incomplete or malformed proof rather than silently dropping a shard."""
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int:
        raise ValueError(f"Invalid report schema: {path}")
    if data["schema_version"] != 1 or not isinstance(data.get("identity"), dict):
        raise ValueError(f"Invalid report identity: {path}")
    raw_identity = data["identity"]
    identity = _identity(
        raw_identity["source_sha"],
        raw_identity["selection"],
        raw_identity["candidate"],
        raw_identity["environment"],
    )
    _shard_identity(data["variant"], data["shard"], data["shard_count"])
    elapsed = _positive_seconds(data["elapsed_seconds"])
    entries = data["cases"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("Report must contain a nonempty case inventory")
    cases = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid case evidence")
        node, outcome = entry.get("id"), entry.get("outcome")
        if (
            not isinstance(node, list)
            or len(node) != 2
            or not all(isinstance(part, str) for part in node)
            or not node[1]
            or not isinstance(outcome, str)
            or outcome not in OUTCOMES
        ):
            raise ValueError("Invalid case identity or outcome")
        key = (node[0], node[1])
        if key in cases:
            raise ValueError(f"Duplicate case evidence: {key!r}")
        cases[key] = outcome
    return Report(identity, data["variant"], data["shard"], data["shard_count"], elapsed, cases)


def _group(reports: list[Report], variant: str) -> dict[tuple[str, str], str]:
    if not reports:
        raise ValueError(f"No {variant} reports")
    count = reports[0].shard_count
    if len(reports) != count or {report.shard for report in reports} != set(range(1, count + 1)):
        raise ValueError(f"Missing or duplicate {variant} shard")
    cases = {}
    for report in reports:
        if report.variant != variant or report.shard_count != count:
            raise ValueError(f"Inconsistent {variant} shard identity")
        if set(cases) & report.cases.keys():
            raise ValueError(f"Overlapping {variant} test partitions")
        if set(report.cases.values()) & {"failed", "error"}:
            raise ValueError(f"{variant} contains failing tests")
        cases.update(report.cases)
    if "passed" not in cases.values():
        raise ValueError(f"{variant} did not execute any passing tests")
    return cases


def compare(
    baseline: list[Report], proposed: list[Report], expected_sha: str, minimum_speedup: float
) -> dict[str, object]:
    """Prove identity, complete selection and outcomes before comparing execution time."""
    require_sha(expected_sha)
    if not math.isfinite(minimum_speedup) or not 0 <= minimum_speedup < 1:
        raise ValueError("Minimum speedup must be finite and in [0, 1)")
    old_cases, new_cases = _group(baseline, "baseline"), _group(proposed, "proposed")
    identity = baseline[0].identity
    if identity["source_sha"] != expected_sha or any(
        report.identity != identity for report in (*baseline, *proposed)
    ):
        raise ValueError(
            "Source, candidate bytes, environment or test selection differ between runs"
        )
    if old_cases != new_cases:
        missing, extra = old_cases.keys() - new_cases.keys(), new_cases.keys() - old_cases.keys()
        changed = sum(old_cases[node] != new_cases[node] for node in old_cases.keys() & new_cases)
        raise ValueError(
            f"Scenario parity failed: {len(missing)} missing, {len(extra)} extra, "
            f"{changed} changed outcomes"
        )
    before = max(report.elapsed_seconds for report in baseline)
    after = max(report.elapsed_seconds for report in proposed)
    speedup = 1 - after / before
    return {
        "schema_version": 1,
        "identity": identity,
        "scenario_count": len(old_cases),
        "outcomes": {
            outcome: list(old_cases.values()).count(outcome) for outcome in sorted(OUTCOMES)
        },
        "same_scenarios_and_outcomes": True,
        "baseline_pytest_critical_seconds": before,
        "proposed_pytest_critical_seconds": after,
        "baseline_pytest_runner_seconds": sum(report.elapsed_seconds for report in baseline),
        "proposed_pytest_runner_seconds": sum(report.elapsed_seconds for report in proposed),
        "speedup_fraction": speedup,
        "minimum_speedup_fraction": minimum_speedup,
        "meets_threshold": speedup >= minimum_speedup,
        "scope": "pytest controller elapsed time; excludes runner setup and queue delays",
    }


def main() -> int:
    """Write durable JSON proof; fail visibly on mismatched evidence or a missed target."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    record = commands.add_parser("capture")
    record.add_argument("--junit", type=Path, required=True)
    record.add_argument("--source-sha", required=True)
    record.add_argument("--selection", required=True)
    record.add_argument("--variant", choices=("baseline", "proposed"), required=True)
    record.add_argument("--shard", type=int, required=True)
    record.add_argument("--shard-count", type=int, required=True)
    record.add_argument("--candidate-metadata", type=Path)
    check = commands.add_parser("compare")
    check.add_argument("--baseline", type=Path, nargs="+", required=True)
    check.add_argument("--proposed", type=Path, nargs="+", required=True)
    check.add_argument("--expected-sha", required=True)
    check.add_argument("--minimum-speedup", type=float, default=0.05)
    for command in (record, check):
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.operation == "capture":
            result = capture(
                args.junit,
                args.source_sha,
                args.selection,
                args.variant,
                args.shard,
                args.shard_count,
                args.candidate_metadata,
            ).payload()
        else:
            result = compare(
                [read_report(path) for path in args.baseline],
                [read_report(path) for path in args.proposed],
                args.expected_sha,
                args.minimum_speedup,
            )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="ascii"
        )
    except (ValueError, KeyError, TypeError, OSError, ET.ParseError) as error:
        parser.error(f"Invalid performance evidence: {error}")
    if args.operation == "compare" and not result["meets_threshold"]:
        parser.exit(1, f"Performance target missed; inspect {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
