"""Required, subprocess-free contracts for lifecycle interaction obligations."""

import gc
import itertools
import json
import shlex
import weakref
import xml.etree.ElementTree as ET
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from tests.utils.lifecycle_interaction_report import main as report_main
from tests.utils.lifecycle_interaction_report import read_executions
from tests.utils.lifecycle_interactions import (
    DYNAMIC_REFUSAL_ROWS,
    HIGH_RISK_TRIPLES,
    INTERACTION_ROWS,
    KNOWN_IDEMPOTENCY_GAP,
    ROUTING_ROWS,
    TRANSITION_ROWS,
    CaseExecution,
    RoutingRow,
    TripleObligation,
    assert_complete_evidence,
    candidate_rows,
    catalog_rows,
    coverage_report,
    execution_from_mapping,
    factors,
    generate_interaction_rows,
    invalid_reason,
    known_gap_for,
    required_interactions,
    required_laws,
    required_transitions,
    validate_campaign,
    validate_routing_rows,
)
from tests.workflow_contracts import load_workflow, shell_tokens, workflow_job, workflow_step

RATCHET_TEST_SCOPE = "repository"
pytestmark = pytest.mark.component
_ALL_ROWS = (*ROUTING_ROWS, *INTERACTION_ROWS)
_REVIEWED_TRIPLE_OBLIGATIONS = {
    (
        "aliased-ref-warm-update",
        (("cache_state", "warm"), ("command", "update"), ("ref_state", "tag")),
    ),
    (
        "user-instructions-reinstall",
        (("command", "reinstall"), ("primitive", "instructions"), ("scope", "user")),
    ),
    (
        "tampered-transitive-audit",
        (("command", "audit"), ("dependency_shape", "transitive"), ("integrity_state", "tampered")),
    ),
}


def _assert_reviewed_triple_obligations(triples: tuple[TripleObligation, ...]) -> None:
    """Keep the reviewed risk content independent of generator/report inputs."""
    actual = {(triple.id, tuple(sorted(triple.values))) for triple in triples}
    assert len(triples) == len(actual) and actual == _REVIEWED_TRIPLE_OBLIGATIONS, (
        "Reviewed high-risk triple obligations changed"
    )


def test_lifecycle_routing_cells_cover_the_live_target_catalog() -> None:
    validate_routing_rows(ROUTING_ROWS)
    validate_campaign(_ALL_ROWS)


@pytest.mark.parametrize("required", (*catalog_rows()[:1], *TRANSITION_ROWS, *DYNAMIC_REFUSAL_ROWS))
def test_routing_ratchet_rejects_missing_cells_and_transitions(required: RoutingRow) -> None:
    with pytest.raises(AssertionError):
        validate_routing_rows(tuple(row for row in ROUTING_ROWS if row.id != required.id))


def test_transition_metadata_cannot_be_replaced_by_a_noop() -> None:
    required = TRANSITION_ROWS[0]
    rows = tuple(
        replace(row, widen_targets=(), narrow_targets=()) if row.id == required.id else row
        for row in ROUTING_ROWS
    )
    with pytest.raises(AssertionError, match="Missing obligation"):
        validate_routing_rows(rows)


def test_target_transitions_cover_both_scopes_without_inventing_global_prune() -> None:
    assert {row.user_scope for row in TRANSITION_ROWS} == {False, True}
    user_row = next(row for row in TRANSITION_ROWS if row.user_scope)
    assert user_row.widen_targets and user_row.narrow_targets
    assert required_transitions(user_row) == {
        "install",
        "widen",
        "reinstall-widened",
        "narrow",
        "uninstall",
    }
    assert all(
        "prune" in required_transitions(row) for row in TRANSITION_ROWS if not row.user_scope
    )


def test_candidate_universe_does_not_hide_valid_nondefault_combinations() -> None:
    """All combinations of these reviewed domains are legal for each static cell."""
    expected_variants = {
        (shape, source, ref, cache, integrity, command)
        for shape, source, integrity, command in itertools.product(
            ("direct", "transitive"),
            ("git", "local"),
            ("clean", "tampered"),
            ("reinstall", "audit", "update"),
        )
        for ref in (("pinned", "tag") if source == "git" else ("none",))
        for cache in (("cold", "warm") if source == "git" else ("none",))
    }
    by_cell: dict[tuple, set[tuple[str, ...]]] = {}
    for row in candidate_rows():
        assert invalid_reason(row) is None
        key = (row.targets, row.primitives, row.user_scope)
        variant = (
            row.dependency_shape,
            row.source_kind,
            row.ref_state,
            row.cache_state,
            row.integrity_state,
            row.command,
        )
        by_cell.setdefault(key, set()).add(variant)
    expected_cells = {(row.targets, row.primitives, row.user_scope) for row in catalog_rows()}
    assert set(by_cell) == expected_cells
    assert all(variants == expected_variants for variants in by_cell.values())


@pytest.mark.parametrize(
    "changes",
    (
        {"source_kind": "local"},
        {"ref_state": "none"},
        {"cache_state": "none"},
        {"command": "unimplemented"},
        {"targets": ("not-a-target",)},
    ),
)
def test_semantic_constraints_reject_impossible_or_unknown_states(
    changes: dict[str, object],
) -> None:
    assert invalid_reason(replace(catalog_rows()[0], **changes)) is not None


def test_selection_is_deterministic_and_covers_ledger_driven_triples() -> None:
    _assert_reviewed_triple_obligations(HIGH_RISK_TRIPLES)
    generate_interaction_rows.cache_clear()
    assert generate_interaction_rows() == INTERACTION_ROWS
    ids = [row.id for row in _ALL_ROWS]
    assert len(ids) == len(set(ids))
    assert len(INTERACTION_ROWS) < len(candidate_rows())
    for triple in HIGH_RISK_TRIPLES:
        assert any(set(triple.values) <= set(factors(row)) for row in _ALL_ROWS), triple.id


@pytest.mark.parametrize(
    ("triple_id", "factor", "substitute"),
    (
        ("aliased-ref-warm-update", "cache_state", "cold"),
        ("user-instructions-reinstall", "scope", "project"),
        ("tampered-transitive-audit", "dependency_shape", "direct"),
    ),
)
@pytest.mark.parametrize("corruption", ("deletion", "legal-substitution"))
def test_reviewed_triple_guard_rejects_deleted_or_substituted_obligations(
    triple_id: str, factor: str, substitute: str, corruption: str
) -> None:
    _assert_reviewed_triple_obligations(HIGH_RISK_TRIPLES)
    original = next(triple for triple in HIGH_RISK_TRIPLES if triple.id == triple_id)
    if corruption == "deletion":
        changed = tuple(triple for triple in HIGH_RISK_TRIPLES if triple.id != triple_id)
    else:
        replacement = replace(
            original,
            values=tuple(
                (name, substitute if name == factor else value) for name, value in original.values
            ),
        )
        assert any(set(replacement.values) <= set(factors(row)) for row in candidate_rows())
        changed = tuple(
            replacement if triple.id == triple_id else triple for triple in HIGH_RISK_TRIPLES
        )
        assert len(changed) == len(HIGH_RISK_TRIPLES)
    with pytest.raises(AssertionError, match="Reviewed high-risk triple obligations changed"):
        _assert_reviewed_triple_obligations(changed)


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("catalog-cold", set()),
        ("catalog-warm", {"remove-materialization", "warm-materialize"}),
        ("generated-cold", {"clear-cache"}),
        ("generated-warm", {"remove-materialization", "warm-materialize"}),
        ("generated-local", set()),
        ("refusal-warm", set()),
        ("generated-refusal-cold", set()),
    ),
)
def test_cache_action_requirements_match_driver_applicability(
    case: str, expected: set[str]
) -> None:
    cell = catalog_rows()[0]
    generated = replace(cell, id="interaction-cache-contract", catalog_cell=False)
    rows = {
        "catalog-cold": cell,
        "catalog-warm": replace(cell, cache_state="warm"),
        "generated-cold": generated,
        "generated-warm": replace(generated, cache_state="warm"),
        "generated-local": replace(
            generated, source_kind="local", ref_state="none", cache_state="none"
        ),
        "refusal-warm": replace(DYNAMIC_REFUSAL_ROWS[0], cache_state="warm"),
        "generated-refusal-cold": replace(
            DYNAMIC_REFUSAL_ROWS[0], id="interaction-refusal-contract"
        ),
    }
    cache_actions = {"remove-materialization", "warm-materialize", "clear-cache"}
    assert required_transitions(rows[case]) & cache_actions == expected


def test_campaign_laws_and_named_exceptions_have_ledger_backing() -> None:
    path = Path(__file__).resolve().parents[1] / "fixtures/lifecycle_bug_ledger.json"
    ledger = json.loads(path.read_text(encoding="ascii"))
    law_ids = {row["id"] for row in ledger["property_catalog"]}
    assert set().union(*(required_laws(row) for row in _ALL_ROWS)) <= law_ids
    issues = {bug["issue"] for bug in ledger["bugs"]}
    assert {triple.issue for triple in HIGH_RISK_TRIPLES if triple.issue is not None} <= issues
    gap = next(gap for gap in ledger["known_gaps"] if gap["id"] == KNOWN_IDEMPOTENCY_GAP)
    assert "idempotency.byte_stable" in gap["properties"]
    assert gap["bounded_by"] and gap["next_decision"]


def _execution(row: RoutingRow, **changes: object) -> CaseExecution:
    evidence = CaseExecution(
        case_id=row.id,
        status="executed",
        evaluated_laws=tuple(sorted(required_laws(row))),
        transitions=tuple(sorted(required_transitions(row))),
        duration_seconds=1.0,
    )
    return replace(evidence, **changes)


def test_generated_rows_never_count_as_executed_coverage() -> None:
    report = coverage_report(_ALL_ROWS, (), input_revision="test-fixture")
    assert report["covered_pairs"] == []
    assert report["uncovered_pairs"] == sorted(required_interactions())
    assert report["unexecuted_case_ids"] == sorted(row.id for row in _ALL_ROWS)
    assert all(not triple["witnesses"] for triple in report["triples"])


def test_complete_execution_accounting_has_deterministic_witnesses() -> None:
    evidence = tuple(_execution(row) for row in _ALL_ROWS)
    report = coverage_report(_ALL_ROWS, evidence, input_revision="test-fixture")
    reverse = coverage_report(_ALL_ROWS, reversed(evidence), input_revision="test-fixture")
    assert json.dumps(report, sort_keys=True) == json.dumps(reverse, sort_keys=True)
    assert report["uncovered_pairs"] == []
    assert report["rejected_evidence"] == {}
    assert report["unexecuted_case_ids"] == []
    assert all(triple["witnesses"] for triple in report["triples"])
    assert_complete_evidence(_ALL_ROWS, evidence)


def test_combined_gate_requires_cases_even_when_all_pairs_have_other_witnesses() -> None:
    omitted = DYNAMIC_REFUSAL_ROWS[0].id
    evidence = tuple(_execution(row) for row in _ALL_ROWS if row.id != omitted)
    report = coverage_report(_ALL_ROWS, evidence, input_revision="test-fixture")
    assert report["uncovered_pairs"] == []
    with pytest.raises(AssertionError, match="Missing lifecycle execution"):
        assert_complete_evidence(_ALL_ROWS, evidence)


def test_known_idempotency_gap_does_not_waive_safety_laws_or_actions() -> None:
    row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    bounded = _execution(
        row,
        status="known_gap",
        evaluated_laws=tuple(sorted(required_laws(row) - {"idempotency.byte_stable"})),
        reason=KNOWN_IDEMPOTENCY_GAP,
    )
    other = tuple(_execution(item) for item in _ALL_ROWS if item.id != row.id)
    assert_complete_evidence(_ALL_ROWS, (*other, bounded))
    for corrupted in (
        replace(bounded, evaluated_laws=()),
        replace(bounded, transitions=()),
        replace(bounded, status="failed"),
        replace(bounded, reason="unreviewed-exception"),
        replace(
            bounded,
            transitions=tuple(step for step in bounded.transitions if step != "converge-known-gap"),
        ),
    ):
        with pytest.raises(AssertionError, match="Unmet lifecycle obligation"):
            assert_complete_evidence(_ALL_ROWS, (*other, corrupted))


def test_known_gap_is_not_inherited_by_new_interaction_shapes() -> None:
    row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    assert known_gap_for(row) == KNOWN_IDEMPOTENCY_GAP
    for changed in (
        replace(row, id="new-interaction"),
        replace(row, command="update"),
        replace(row, dependency_shape="transitive"),
        replace(row, integrity_state="tampered"),
    ):
        assert known_gap_for(changed) is None


def test_tracked_product_bugs_do_not_become_coverage_exemptions() -> None:
    path = Path(__file__).resolve().parents[1] / "fixtures/lifecycle_bug_ledger.json"
    ledger = json.loads(path.read_text(encoding="ascii"))
    tracked = [gap for gap in ledger["known_gaps"] if "issue" in gap]
    assert {2815, 2816} <= {gap["issue"] for gap in tracked}
    rows = {row.id: row for row in _ALL_ROWS}
    for gap in tracked:
        assert gap["regression_case"].startswith(gap["bounded_by"] + "[")
        if gap["interaction_case"] is not None:
            row = rows[gap["interaction_case"]]
            assert known_gap_for(row) is None
            evidence = tuple(
                _execution(item, status="known_gap", reason=gap["id"])
                if item.id == row.id
                else _execution(item)
                for item in _ALL_ROWS
            )
            with pytest.raises(AssertionError, match="Unmet lifecycle obligation"):
                assert_complete_evidence(_ALL_ROWS, evidence)


def test_known_gap_cannot_be_applied_to_unrelated_cells() -> None:
    row = ROUTING_ROWS[0]
    assert row.id != "copilot-instructions-user"
    evidence = tuple(
        _execution(item, status="known_gap", reason=KNOWN_IDEMPOTENCY_GAP)
        if item.id == row.id
        else _execution(item)
        for item in _ALL_ROWS
    )
    with pytest.raises(AssertionError, match="Unmet lifecycle obligation"):
        assert_complete_evidence(_ALL_ROWS, evidence)


def test_combined_gate_requires_executed_triples(monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = tuple(_execution(row) for row in _ALL_ROWS)
    report = coverage_report(_ALL_ROWS, evidence, input_revision="test-fixture")
    report["triples"][0]["witnesses"] = []
    monkeypatch.setattr(
        "tests.utils.lifecycle_interactions.coverage_report", lambda *args, **kwargs: report
    )
    with pytest.raises(AssertionError, match="Uncovered high-risk triples"):
        assert_complete_evidence(_ALL_ROWS, evidence)


@pytest.mark.parametrize("status", ("skipped", "setup_failed", "failed", "known_gap"))
def test_nonexecuted_or_known_gap_evidence_earns_no_credit(status: str) -> None:
    row = INTERACTION_ROWS[0]
    report = coverage_report(
        _ALL_ROWS, (_execution(row, status=status, reason="fixture"),), input_revision="fixture"
    )
    assert report["covered_pairs"] == []
    assert report["rejected_evidence"][row.id]["status"] == status


def test_tag_advancement_is_required_only_for_tagged_updates() -> None:
    assert {row.id for row in _ALL_ROWS if "advance-tag" in required_transitions(row)} == {
        row.id for row in _ALL_ROWS if row.command == "update" and row.ref_state == "tag"
    }


@pytest.mark.parametrize(
    ("field", "cache_state", "omitted_action"),
    (
        ("evaluated_laws", None, None),
        ("transitions", None, None),
        ("transitions", "warm", "remove-materialization"),
        ("transitions", "warm", "warm-materialize"),
        ("transitions", "cold", "clear-cache"),
        ("transitions", "warm", "advance-tag"),
    ),
)
def test_disabling_assertions_or_actions_loses_execution_credit(
    tmp_path: Path, field: str, cache_state: str | None, omitted_action: str | None
) -> None:
    row = next(
        row
        for row in INTERACTION_ROWS
        if (cache_state is None or row.cache_state == cache_state)
        and (
            omitted_action != "advance-tag" or (row.command == "update" and row.ref_state == "tag")
        )
    )
    complete = _execution(row)
    if omitted_action is not None:
        complete = replace(
            complete, transitions=tuple(sorted({*complete.transitions, omitted_action}))
        )
    positive = coverage_report(
        _ALL_ROWS, _roundtrip_execution(tmp_path, complete), input_revision="test-fixture"
    )
    assert positive["covered_pairs"]
    assert positive["rejected_evidence"] == {}
    if omitted_action == "advance-tag":
        triple = next(
            triple for triple in positive["triples"] if triple["id"] == "aliased-ref-warm-update"
        )
        assert triple["witnesses"] == [row.id]
    remaining = (
        tuple(step for step in complete.transitions if step != omitted_action)
        if omitted_action is not None
        else ()
    )
    incomplete = _roundtrip_execution(tmp_path, replace(complete, **{field: remaining}))
    report = coverage_report(_ALL_ROWS, incomplete, input_revision="test-fixture")
    assert report["covered_pairs"] == []
    assert all(not triple["witnesses"] for triple in report["triples"])
    assert row.id in report["rejected_evidence"]
    if omitted_action is not None:
        rejection = report["rejected_evidence"][row.id]
        assert rejection["missing_transitions"] == [omitted_action]
        assert rejection["missing_laws"] == []
    with pytest.raises(AssertionError, match="Unmet lifecycle obligation"):
        assert_complete_evidence(
            _ALL_ROWS,
            (*(_execution(item) for item in _ALL_ROWS if item.id != row.id), *incomplete),
        )


def _roundtrip_execution(path: Path, evidence: CaseExecution) -> tuple[CaseExecution, ...]:
    """Exercise the real JUnit reader before assigning interaction credit."""
    root = ET.Element("testsuite")
    case = ET.SubElement(root, "testcase", name=evidence.case_id)
    properties = ET.SubElement(case, "properties")
    ET.SubElement(
        properties, "property", name="lifecycle_execution", value=json.dumps(asdict(evidence))
    )
    junit = path / "cache-contract.xml"
    ET.ElementTree(root).write(junit)
    observed = read_executions(junit)
    assert observed == (evidence,)
    return observed


def test_report_rejects_duplicate_unknown_and_invalid_execution_records() -> None:
    evidence = _execution(INTERACTION_ROWS[0])
    for invalid in (
        (evidence, evidence),
        (replace(evidence, case_id="unknown"),),
        (replace(evidence, status="pretend-pass"),),
        (replace(evidence, duration_seconds=-1),),
        (replace(evidence, duration_seconds=float("nan")),),
    ):
        with pytest.raises(ValueError):
            coverage_report(_ALL_ROWS, invalid, input_revision="test-fixture")


def test_execution_artifact_parser_rejects_malformed_evidence() -> None:
    payload = {
        "case_id": "case",
        "status": "executed",
        "evaluated_laws": ["law"],
        "transitions": ["install"],
        "duration_seconds": 1,
    }
    assert execution_from_mapping(payload).case_id == "case"
    for field, value in (
        ("case_id", None),
        ("evaluated_laws", "not-an-array"),
        ("transitions", [1]),
        ("duration_seconds", True),
        ("reason", []),
    ):
        with pytest.raises(ValueError):
            execution_from_mapping({**payload, field: value})


@pytest.mark.parametrize("pytest_status", ("failure", "error", "skipped", None))
def test_junit_result_overrules_earlier_success_evidence(
    tmp_path: Path, pytest_status: str | None
) -> None:
    root = ET.Element("testsuite")
    case = ET.SubElement(root, "testcase", name="lifecycle")
    properties = ET.SubElement(case, "properties")
    evidence = _execution(INTERACTION_ROWS[0])
    payload = {
        "case_id": evidence.case_id,
        "status": evidence.status,
        "evaluated_laws": list(evidence.evaluated_laws),
        "transitions": list(evidence.transitions),
        "duration_seconds": evidence.duration_seconds,
    }
    ET.SubElement(properties, "property", name="lifecycle_execution", value=json.dumps(payload))
    if pytest_status:
        ET.SubElement(case, pytest_status)
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path)
    observed = read_executions(path)
    expected = (
        "executed"
        if pytest_status is None
        else ("skipped" if pytest_status == "skipped" else "failed")
    )
    assert observed[0].status == expected
    report = coverage_report(_ALL_ROWS, observed, input_revision="test-fixture")
    assert bool(report["covered_pairs"]) is (pytest_status is None)


@pytest.mark.parametrize("pytest_status", ("failure", "error", "skipped"))
@pytest.mark.parametrize("reason", (None, "audit: missing authorized fixture"))
def test_junit_veto_preserves_original_diagnostic_and_artifact_context(
    tmp_path: Path, pytest_status: str, reason: str | None
) -> None:
    root = ET.Element("testsuite")
    case = ET.SubElement(root, "testcase", classname="fixture.module", name="audit-case")
    properties = ET.SubElement(case, "properties")
    evidence = _execution(INTERACTION_ROWS[0], reason=reason)
    ET.SubElement(
        properties, "property", name="lifecycle_execution", value=json.dumps(asdict(evidence))
    )
    ET.SubElement(case, pytest_status, message="final assertion").text = "raw assertion detail"
    path = tmp_path / "shard.xml"
    ET.ElementTree(root).write(path)
    execution = read_executions(path)[0]
    assert execution.status == ("skipped" if pytest_status == "skipped" else "failed")
    expected = (
        f"pytest-{pytest_status}: final assertion; raw assertion detail; "
        f"artifact={path.as_posix()}; testcase=fixture.module::audit-case"
    )
    assert execution.reason == (f"{reason}; {expected}" if reason else expected)
    report = coverage_report(_ALL_ROWS, (execution,), input_revision="test-fixture")
    assert report["rejected_evidence"][evidence.case_id]["reason"] == execution.reason
    assert report["covered_pairs"] == []


@pytest.mark.parametrize("artifact_count", (2, 20))
@pytest.mark.parametrize("malformed_mutations", (False, True))
def test_combined_report_parses_each_artifact_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    artifact_count: int,
    malformed_mutations: bool,
) -> None:
    from tests.utils.lifecycle_mutations import mutation_report

    shard_dir = tmp_path / "shards"
    shard_dir.mkdir()
    expected_executions = []
    paths = []
    for index, row in enumerate(_ALL_ROWS[:artifact_count]):
        root = ET.Element("testsuite")
        case = ET.SubElement(root, "testcase", name=row.id)
        properties = ET.SubElement(case, "properties")
        ET.SubElement(
            properties,
            "property",
            name="lifecycle_execution",
            value=json.dumps(asdict(_execution(row))),
        )
        if malformed_mutations:
            ET.SubElement(properties, "property", name="lifecycle_mutation", value="{}")
        if index == 0:
            ET.SubElement(case, "failure", message="later failure")
        path = shard_dir / f"shard-{index:02d}.xml"
        ET.ElementTree(root).write(path)
        paths.append(path)
        expected_executions.extend(read_executions(path))
    output, mutations = tmp_path / "interactions.json", tmp_path / "mutations.json"
    parse = ET.parse
    parsed_paths = []
    parsed_roots = []

    def counted_parse(path: Path) -> ET.ElementTree:
        parsed_paths.append(path)
        tree = parse(path)
        parsed_roots.append(weakref.ref(tree.getroot()))
        gc.collect()
        assert sum(reference() is not None for reference in parsed_roots) <= 2, (
            "Reporter retained prior JUnit roots during ingestion"
        )
        return tree

    monkeypatch.setattr(ET, "parse", counted_parse)
    monkeypatch.setattr(
        "sys.argv",
        [
            "lifecycle-report",
            "--junit-dir",
            str(shard_dir),
            "--revision",
            "test-fixture",
            "--output",
            str(output),
            "--mutation-output",
            str(mutations),
        ],
    )
    if malformed_mutations:
        with pytest.raises(ValueError, match="Invalid lifecycle mutation artifacts:") as error:
            report_main()
        assert "Malformed lifecycle mutation report" in str(error.value)
        assert all(str(path) in str(error.value) for path in paths)
        assert not mutations.exists()
    else:
        report_main()
    assert parsed_paths == paths
    gc.collect()
    assert sum(reference() is not None for reference in parsed_roots) <= 1, (
        "Reporter retained prior JUnit roots after ingestion"
    )
    report = json.loads(output.read_text(encoding="ascii"))
    expected = coverage_report(_ALL_ROWS, expected_executions, input_revision="test-fixture")
    assert {key: report[key] for key in expected} == json.loads(json.dumps(expected))
    assert report["executions"] == json.loads(
        json.dumps(
            [
                asdict(execution)
                for execution in sorted(expected_executions, key=lambda e: e.case_id)
            ]
        )
    )
    if not malformed_mutations:
        assert json.loads(mutations.read_text(encoding="ascii")) == {
            **mutation_report(()),
            "input_revision": "test-fixture",
        }


@pytest.mark.parametrize("invalid_index", (0, 1))
def test_invalid_mutation_still_preserves_complete_interaction_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalid_index: int
) -> None:
    paths = []
    rows = _ALL_ROWS[:2]
    for index, row in enumerate(rows):
        root = ET.Element("testsuite")
        case = ET.SubElement(root, "testcase", name=row.id)
        properties = ET.SubElement(case, "properties")
        ET.SubElement(
            properties,
            "property",
            name="lifecycle_execution",
            value=json.dumps(asdict(_execution(row))),
        )
        if index == invalid_index:
            ET.SubElement(properties, "property", name="lifecycle_mutation", value="{}")
        path = tmp_path / f"shard-{index}.xml"
        ET.ElementTree(root).write(path)
        paths.append(path)
    output, mutations = tmp_path / "interactions.json", tmp_path / "mutations.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lifecycle-report",
            "--junit",
            *(str(path) for path in paths),
            "--revision",
            "test-fixture",
            "--output",
            str(output),
            "--mutation-output",
            str(mutations),
            "--require-mutations",
            "--require-complete",
        ],
    )
    with pytest.raises(ValueError, match="Malformed lifecycle mutation report") as error:
        report_main()
    assert str(paths[invalid_index]) in str(error.value)
    assert output.is_file(), "Valid interaction evidence was not saved before mutation failure"
    report = json.loads(output.read_text(encoding="ascii"))
    assert {execution["case_id"] for execution in report["executions"]} == {row.id for row in rows}
    assert not mutations.exists()


def test_ci_uploads_observed_lifecycle_evidence_even_after_test_failure() -> None:
    path = Path(__file__).resolve().parents[2] / ".github/workflows/ci-integration.yml"
    workflow = load_workflow(path)
    assert set(workflow["on"]) == {"merge_group"}
    job = workflow_job(workflow, "integration-tests-shard")
    run = workflow_step(job, "Run integration tests (sharded + parallelized)")
    assert "--junitxml=lifecycle-junit.xml" in shlex.split(run["env"]["PYTEST_EXTRA_ARGS"])
    assert "junit_family=legacy" in shlex.split(run["env"]["PYTEST_EXTRA_ARGS"])
    report = workflow_step(job, "Report observed lifecycle interactions")
    assert report["if"] == "always() && hashFiles('lifecycle-junit.xml') != ''"
    assert shell_tokens(report) == [
        "uv",
        "run",
        "--frozen",
        "--extra",
        "dev",
        "python",
        "-m",
        "tests.utils.lifecycle_interaction_report",
        "--junit",
        "lifecycle-junit.xml",
        "--revision",
        "${{ github.sha }}",
        "--output",
        "lifecycle-interactions.json",
        "--mutation-output",
        "lifecycle-mutations.json",
    ]
    upload = workflow_step(job, "Upload lifecycle evidence")
    assert upload["if"] == "always()"
    assert set(upload["with"]["path"].splitlines()) == {
        "lifecycle-junit.xml",
        "lifecycle-interactions.json",
        "lifecycle-mutations.json",
    }
    assert upload["with"]["retention-days"] >= 7
    fan_in = workflow_job(workflow, "integration-tests")
    install = workflow_step(fan_in, "Install lifecycle report dependencies")
    assert shell_tokens(install) == ["uv", "sync", "--frozen"]
    assert sum(step.get("name") == install["name"] for step in fan_in["steps"]) == 1
    download = workflow_step(fan_in, "Download lifecycle evidence")
    assert download["with"]["pattern"] == "lifecycle-evidence-shard-*"
    assert download["with"].get("merge-multiple", False) is False
    combined = workflow_step(fan_in, "Enforce combined lifecycle evidence")
    assert combined["if"] == "always()"
    assert combined.get("continue-on-error", False) is False
    assert "--require-complete" in shell_tokens(combined)
    assert "--junit-dir" in shell_tokens(combined)
    assert "--require-mutations" in shell_tokens(combined)
    assert "--mutation-output" in shell_tokens(combined)
    assert shell_tokens(combined)[:5] == ["uv", "run", "--frozen", "--no-sync", "python"]
    combined_upload = workflow_step(fan_in, "Upload combined lifecycle evidence")
    assert combined_upload["if"] == "always()"
    assert set(combined_upload["with"]["path"].splitlines()) == {
        "lifecycle-interactions-combined.json",
        "lifecycle-mutations-combined.json",
    }


def test_report_command_persists_missing_evidence_before_failing_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junit_dir = tmp_path / "shards"
    junit_dir.mkdir()
    ET.ElementTree(ET.Element("testsuite")).write(junit_dir / "junit.xml")
    output = tmp_path / "combined.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lifecycle-report",
            "--junit-dir",
            str(junit_dir),
            "--revision",
            "test-fixture",
            "--output",
            str(output),
            "--require-complete",
        ],
    )
    with pytest.raises(AssertionError, match="Missing lifecycle execution"):
        report_main()
    report = json.loads(output.read_text(encoding="ascii"))
    assert report["input_revision"] == "test-fixture"
    assert report["covered_pairs"] == []
    assert report["unexecuted_case_ids"] == sorted(row.id for row in _ALL_ROWS)
    assert {bug["issue"] for bug in report["tracked_product_bugs"]} >= {2815, 2816}
