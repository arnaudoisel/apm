"""Combine real pytest lifecycle witnesses without crediting unexecuted cases."""

from __future__ import annotations

import argparse
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from pathlib import Path

from tests.utils.lifecycle_interactions import (
    INTERACTION_ROWS,
    ROUTING_ROWS,
    CaseExecution,
    assert_complete_evidence,
    coverage_report,
    execution_from_mapping,
)
from tests.utils.lifecycle_mutations import (
    assert_complete_mutation_evidence,
    read_mutation_results_from_root,
)
from tests.utils.lifecycle_mutations import (
    write_report as write_mutation_report,
)


def read_executions(path: Path) -> tuple[CaseExecution, ...]:
    """Parse JUnit once and read its explicit interaction witnesses."""
    root = ET.parse(path).getroot()  # noqa: S314 - Input is local pytest-generated JUnit.
    return read_executions_from_root(root, path=path)


def read_executions_from_root(root: ET.Element, *, path: Path) -> tuple[CaseExecution, ...]:
    """Read witnesses without reparsing, retaining diagnostics when pytest vetoes credit."""
    executions = []
    for case in root.iter("testcase"):
        properties = [
            prop.get("value")
            for prop in case.findall("./properties/property")
            if prop.get("name") == "lifecycle_execution"
        ]
        if not properties:
            continue
        if len(properties) != 1 or properties[0] is None:
            raise ValueError(f"Ambiguous lifecycle evidence in {path}: {case.get('name')}")
        payload = json.loads(properties[0])
        if not isinstance(payload, dict):
            raise ValueError(f"Lifecycle evidence in {path} must be an object")
        execution = execution_from_mapping(payload)
        outcome = next(
            (kind for kind in ("failure", "error", "skipped") if case.find(kind) is not None),
            None,
        )
        if outcome is not None:
            node = case.find(outcome)
            assert node is not None
            detail = "; ".join(
                part.strip() for part in (node.get("message", ""), node.text or "") if part.strip()
            )
            identity = "::".join(part for part in (case.get("classname"), case.get("name")) if part)
            veto = (
                f"pytest-{outcome}: {detail or 'no JUnit message'}; "
                f"artifact={path.as_posix()}; testcase={identity or 'unnamed'}"
            )
            execution = replace(
                execution,
                status="skipped" if outcome == "skipped" else "failed",
                reason="; ".join(part for part in (execution.reason, veto) if part),
            )
        executions.append(execution)
    return tuple(executions)


def main() -> None:
    """Render a deterministic report from one shard or a combined set of JUnit files."""
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--junit", type=Path, nargs="+")
    inputs.add_argument("--junit-dir", type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--mutation-output", type=Path)
    parser.add_argument("--require-mutations", action="store_true")
    arguments = parser.parse_args()
    if arguments.require_mutations and arguments.mutation_output is None:
        parser.error("--require-mutations needs --mutation-output")
    paths = (
        sorted(arguments.junit_dir.rglob("*.xml"))
        if arguments.junit_dir is not None
        else arguments.junit
    )
    if not paths:
        raise ValueError("No lifecycle JUnit artifacts found")
    executions = []
    mutation_results = []
    mutation_errors: list[tuple[Path, ValueError]] = []
    for path in paths:
        root = ET.parse(path).getroot()  # noqa: S314 - Local pytest-generated JUnit.
        executions.extend(read_executions_from_root(root, path=path))
        if arguments.mutation_output is not None:
            try:
                mutation_results.extend(read_mutation_results_from_root(root, path=path))
            except ValueError as error:
                mutation_errors.append((path, error))
    report = coverage_report(
        (*ROUTING_ROWS, *INTERACTION_ROWS),
        executions,
        input_revision=arguments.revision,
    )
    report["executions"] = [
        asdict(execution) for execution in sorted(executions, key=lambda e: e.case_id)
    ]
    ledger_path = Path(__file__).resolve().parents[1] / "fixtures/lifecycle_bug_ledger.json"
    ledger = json.loads(ledger_path.read_text(encoding="ascii"))
    report["tracked_product_bugs"] = [
        {key: gap[key] for key in ("id", "issue", "regression_case", "interaction_case")}
        for gap in ledger["known_gaps"]
        if "issue" in gap
    ]
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(
        json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="ascii"
    )
    print(
        f"Lifecycle pairs: {len(report['covered_pairs'])}/{len(report['valid_pairs'])}; "
        f"unexecuted cases: {len(report['unexecuted_case_ids'])}; "
        f"rejected evidence: {len(report['rejected_evidence'])}"
    )
    if mutation_errors:
        details = "; ".join(f"{path}: {error}" for path, error in mutation_errors)
        raise ValueError(f"Invalid lifecycle mutation artifacts: {details}") from mutation_errors[
            0
        ][1]
    if arguments.mutation_output is not None:
        mutation_report = write_mutation_report(
            arguments.mutation_output,
            mutation_results,
            input_revision=arguments.revision,
        )
        counts = mutation_report["counts"]
        print(
            f"Lifecycle source mutants: {counts['killed']} killed; "
            f"{counts['survived']} survived; {counts['error']} errors; "
            f"{len(mutation_report['unexecuted_mutant_ids'])} missing"
        )
        if arguments.require_mutations:
            assert_complete_mutation_evidence(mutation_results)
    if arguments.require_complete:
        assert_complete_evidence((*ROUTING_ROWS, *INTERACTION_ROWS), executions)


if __name__ == "__main__":
    main()
