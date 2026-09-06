"""Fast mutation-accounting contracts; no installed CLI or E2E prerequisites."""

from __future__ import annotations

import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.utils.lifecycle_mutations import (
    ISOLATION_CHECKS,
    MUTATIONS,
    LifecycleMutation,
    RunObservation,
    assert_complete_mutation_evidence,
    child_main,
    classify_mutation,
    mutation_report,
    observe_run,
    read_mutation_results,
    read_mutation_results_from_root,
    write_report,
)

pytestmark = [pytest.mark.component, pytest.mark.lifecycle_smoke]


def _observation(
    mutation: LifecycleMutation = MUTATIONS[0],
    *,
    mutant: bool = False,
    outcome: str = "executed",
    project: Path | None = None,
) -> RunObservation:
    project = project or Path(Path.cwd().anchor) / "fixture"
    context = {
        "mutant_id": mutation.id,
        "mode": "mutant" if mutant else "baseline",
        "command": ["install"],
    }
    original = (project / ".agents/skills").as_posix()
    replacement = (project / ".claude/skills").as_posix()
    destination = replacement if mutant else original
    effect = (
        {
            "original": original,
            "replacement": replacement,
            "written": [f"{destination}/{Path(mutation.witness_path).parent.name}"],
        }
        if mutation.id == "wrong-target-deployment"
        else {"paths": [mutation.witness_path], "existed": True}
    )
    detail = (
        f"missing={{('copilot', '{mutation.witness_path}')}}, unexpected=set()"
        if mutation.id == "omitted-ledger-claim"
        else mutation.witness_path
    )
    return RunObservation(
        outcome,
        f"AssertionError: {mutation.failure_prefix} {detail}" if mutant else "",
        mutation.failure_file if mutant else "",
        mutation.failure_function if mutant else "",
        0.25,
        (
            {**context, "event": "command_start", "cwd": project.as_posix()},
            {**context, **effect, "event": "reach", "changed": mutant},
            {**context, "event": "command_end", "returncode": 0},
        ),
    )


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda mutation: mutation.id)
def test_only_intended_reached_failure_is_killed(mutation: LifecycleMutation) -> None:
    """A green baseline plus a located intended assertion is positive evidence."""
    result = classify_mutation(
        mutation, _observation(mutation), _observation(mutation, mutant=True, outcome="assertion")
    )
    assert result["status"] == "killed"
    assert result["reached"] is True
    assert result["baseline_green"] is True
    assert result["rejection_reason"] == ""


def test_wrong_target_matches_real_missing_deployment_traceback(tmp_path: Path) -> None:
    """Use the actual oracle assertion rather than copying catalog frame names."""
    from tests.utils.lifecycle_interaction_oracle import InteractionOracle, SourceFixture
    from tests.utils.lifecycle_interactions import ROUTING_ROWS

    mutation = MUTATIONS[0]
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    row = next(row for row in ROUTING_ROWS if row.id == mutation.case_id)
    source = SourceFixture(
        "fixture",
        "skills",
        Path(mutation.witness_path).parent.name,
        "fixture-marker",
        ("skills/fixture/SKILL.md",),
        "fixture-org/fixture",
    )
    oracle = InteractionOracle(
        {"project": project, "user": home}, "project", project, (source,), row
    )
    observation = observe_run(
        lambda: oracle.assert_routing(row.targets), tmp_path / "no-command-events.jsonl"
    )
    assert observation.outcome == "assertion"
    assert (
        observation.diagnostic
        == f"AssertionError: {mutation.failure_prefix} {mutation.witness_path}"
    )
    assert observation.failure_file == mutation.failure_file
    assert observation.failure_function == mutation.failure_function
    reached = replace(observation, events=_observation(mutant=True, project=project).events)
    assert classify_mutation(mutation, _observation(project=project), reached)["status"] == "killed"


@pytest.mark.parametrize("mode", ("baseline", "mutant"))
@pytest.mark.parametrize(
    "fault",
    (
        "sibling-skill",
        "unrelated-written-root",
        "unrelated-fixture",
        "wrong-original",
        "wrong-replacement",
        "missing-cwd",
        "relative-cwd",
        "wrong-cwd",
    ),
)
def test_wrong_target_effect_requires_exact_skill_and_command_project(
    tmp_path: Path, mode: str, fault: str
) -> None:
    """Neither mode can borrow a neighboring skill or another fixture's write."""
    mutation = MUTATIONS[0]
    baseline = _observation(project=tmp_path / "baseline")
    mutant = _observation(mutant=True, outcome="assertion", project=tmp_path / "mutant")
    observation = baseline if mode == "baseline" else mutant
    start, reach, end = (dict(event) for event in observation.events)
    if fault == "sibling-skill":
        reach["written"] = [Path(reach["written"][0]).with_name("unrelated-skill").as_posix()]
    elif fault == "unrelated-written-root":
        reach["written"] = [(tmp_path / "unrelated" / Path(reach["written"][0]).name).as_posix()]
    elif fault == "unrelated-fixture":
        # Keep both target roots and the exact skill mutually consistent, but
        # move all effect claims outside the actual command's fixture.
        project = tmp_path / "unrelated"
        reach["original"] = (project / ".agents/skills").as_posix()
        reach["replacement"] = (project / ".claude/skills").as_posix()
        destination = reach["original"] if mode == "baseline" else reach["replacement"]
        reach["written"] = [
            (Path(destination) / Path(mutation.witness_path).parent.name).as_posix()
        ]
    elif fault in {"wrong-original", "wrong-replacement"}:
        reach[fault.removeprefix("wrong-")] = (tmp_path / mode / ".cursor/skills").as_posix()
    elif fault == "missing-cwd":
        start.pop("cwd")
    elif fault == "relative-cwd":
        start["cwd"] = "relative-fixture"
    else:
        start["cwd"] = (tmp_path / "unrelated").as_posix()
    broken = replace(observation, events=(start, reach, end))
    result = classify_mutation(
        mutation,
        broken if mode == "baseline" else baseline,
        broken if mode == "mutant" else mutant,
    )
    assert result["status"] == "error"
    assert result["rejection_reason"] == (
        f"effect-not-observed: {mode} requires {mutation.witness_path}"
    )
    assert result["failure_diagnostic"] == mutant.diagnostic


def test_wrong_target_accepts_distinct_actual_fixture_projects(tmp_path: Path) -> None:
    """Baseline and mutant are isolated siblings, never required to share cwd."""
    result = classify_mutation(
        MUTATIONS[0],
        _observation(project=tmp_path / "baseline" / "consumer"),
        _observation(mutant=True, outcome="assertion", project=tmp_path / "mutant" / "consumer"),
    )
    assert result["status"] == "killed"
    assert result["rejection_reason"] == ""


def test_child_command_start_records_actual_fixture_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Readback's fixture boundary comes from invocation cwd, not the reach claim."""
    import apm_cli

    monkeypatch.chdir(tmp_path)
    monkeypatch.setitem(sys.modules, "sitecustomize", SimpleNamespace())
    entry = SimpleNamespace(
        group="console_scripts", name="apm", value="apm_cli.cli:main", load=lambda: lambda: 0
    )
    monkeypatch.setattr(
        "tests.utils.lifecycle_mutations.importlib.metadata.distribution",
        lambda name: SimpleNamespace(entry_points=(entry,)),
    )
    monkeypatch.setattr(
        "tests.utils.lifecycle_mutations._install_probe", lambda *args, **kwargs: None
    )
    monkeypatch.setattr("sys.argv", [])
    events = tmp_path / "events.jsonl"
    returncode = child_main(
        [
            MUTATIONS[0].id,
            "baseline",
            str(events),
            str(Path(apm_cli.__file__).resolve().parent),
            "install",
        ]
    )
    recorded = [json.loads(line) for line in events.read_text(encoding="ascii").splitlines()]
    assert returncode == 0
    assert [event["event"] for event in recorded] == ["command_start", "command_end"]
    assert recorded[0].get("cwd") == tmp_path.resolve().as_posix()
    assert recorded[0]["command"] == ["install"]


@pytest.mark.parametrize(
    "fault",
    (
        "baseline-red",
        "baseline-unreached",
        "baseline-cli-error",
        "unreached",
        "cli-error",
        "incomplete-command",
        "wrong-command",
        "wrong-assertion",
        "wrong-file",
        "wrong-function",
        "wrong-path",
        "exception",
        "timeout",
        "collection-error",
        "wrong-mutant",
        "wrong-mode",
        "reach-outside-command",
        "reversed-command",
        "boolean-exit",
        "empty-command",
        "missing-effect",
        "baseline-mutated",
        "neighbor-path",
    ),
)
def test_infrastructure_and_unrelated_failures_are_errors(fault: str) -> None:
    """Never import the pilot's mutmut-only nonzero-exit-to-kill mapping."""
    baseline = _observation()
    mutant = _observation(mutant=True, outcome="assertion")
    if fault == "baseline-red":
        baseline = replace(baseline, outcome="assertion")
    elif fault == "baseline-unreached":
        baseline = replace(baseline, events=(baseline.events[0], baseline.events[-1]))
    elif fault in {"baseline-cli-error", "cli-error"}:
        original = baseline if fault == "baseline-cli-error" else mutant
        broken = replace(
            original, events=(*original.events[:-1], {**original.events[-1], "returncode": 1})
        )
        if fault == "baseline-cli-error":
            baseline = broken
        else:
            mutant = broken
    elif fault == "unreached":
        mutant = replace(mutant, events=(mutant.events[0], mutant.events[-1]))
    elif fault == "incomplete-command":
        mutant = replace(mutant, events=mutant.events[:-1])
    elif fault == "wrong-command":
        mutant = replace(
            mutant, events=(*mutant.events[:-1], {**mutant.events[-1], "command": ["uninstall"]})
        )
    elif fault == "wrong-assertion":
        mutant = replace(mutant, diagnostic="AssertionError: install: exit=1")
    elif fault == "wrong-file":
        mutant = replace(mutant, failure_file="test_unrelated.py")
    elif fault == "wrong-function":
        mutant = replace(mutant, failure_function="unrelated_assertion")
    elif fault == "wrong-path":
        mutant = replace(
            mutant, diagnostic=f"AssertionError: {MUTATIONS[0].failure_prefix} ['unrelated']"
        )
    elif fault == "neighbor-path":
        mutant = replace(mutant, diagnostic=mutant.diagnostic + "-neighbor")
    elif fault in {"wrong-mutant", "wrong-mode", "empty-command", "boolean-exit"}:
        key, value = {
            "wrong-mutant": ("mutant_id", MUTATIONS[1].id),
            "wrong-mode": ("mode", "baseline"),
            "empty-command": ("command", []),
            "boolean-exit": ("returncode", False),
        }[fault]
        mutant = replace(mutant, events=tuple({**event, key: value} for event in mutant.events))
    elif fault == "reach-outside-command":
        mutant = replace(mutant, events=(mutant.events[1], mutant.events[0], mutant.events[2]))
    elif fault == "reversed-command":
        mutant = replace(mutant, events=tuple(reversed(mutant.events)))
    elif fault == "missing-effect":
        mutant = replace(
            mutant,
            events=(mutant.events[0], {**mutant.events[1], "written": []}, mutant.events[2]),
        )
    elif fault == "baseline-mutated":
        baseline = replace(
            baseline,
            events=(
                baseline.events[0],
                {**baseline.events[1], "changed": True},
                baseline.events[2],
            ),
        )
    else:
        mutant = replace(mutant, outcome="error", diagnostic=fault)
    result = classify_mutation(MUTATIONS[0], baseline, mutant)
    assert result["status"] == "error"
    if fault == "baseline-red":
        reason = "baseline-not-green:"
    elif fault in {"baseline-cli-error", "baseline-mutated"}:
        reason = "invalid-command-trace: baseline"
    elif fault == "baseline-unreached":
        reason = "effect-not-observed: baseline"
    elif fault in {"unreached", "missing-effect"}:
        reason = "effect-not-observed: mutant"
    elif fault in {
        "cli-error",
        "incomplete-command",
        "wrong-command",
        "wrong-mutant",
        "wrong-mode",
        "reach-outside-command",
        "reversed-command",
        "boolean-exit",
        "empty-command",
    }:
        reason = "invalid-command-trace: mutant"
    else:
        reason = "unintended-oracle-failure:"
    assert result["rejection_reason"].startswith(reason)
    assert result["failure_diagnostic"] == mutant.diagnostic


def test_reached_mutation_with_passing_oracle_survives() -> None:
    """Survival is visible, never silently accepted into an allowlist."""
    result = classify_mutation(MUTATIONS[0], _observation(), _observation(mutant=True))
    assert result["status"] == "survived"
    assert result["rejection_reason"].startswith("mutation-survived:")


def test_runtime_exception_is_not_an_assertion_kill(tmp_path: Path) -> None:
    """Runner timeouts remain errors even if a mutation was previously reached."""

    def timeout() -> None:
        raise subprocess.TimeoutExpired(("apm", "install"), 1)

    observation = observe_run(timeout, tmp_path / "absent.jsonl")
    assert observation.outcome == "error"
    assert observation.diagnostic.startswith("TimeoutExpired:")
    assert observation.events == ()


def test_report_is_sorted_ascii_and_complete(tmp_path: Path) -> None:
    """Reuse atomic report conventions without executing or importing mutmut."""
    result = classify_mutation(
        MUTATIONS[0], _observation(), _observation(mutant=True, outcome="assertion")
    )
    path = tmp_path / "report.json"
    report = write_report(path, (result,))
    first = path.read_bytes()
    write_report(path, (result,))
    assert path.read_bytes() == first
    assert all(byte < 128 for byte in first)
    assert b"\r" not in first
    assert json.loads(first) == report
    assert report["counts"] == {"killed": 1, "survived": 0, "error": 0, "total": 1}
    assert "not a frozen binary" in report["execution_boundary"]


def test_catalog_is_exactly_three_non_equivalent_green_cases() -> None:
    """The bounded campaign must not absorb known-red or user-relative cases."""
    assert {mutation.id for mutation in MUTATIONS} == {
        "wrong-target-deployment",
        "omitted-ledger-claim",
        "skipped-target-cleanup",
    }
    assert len({mutation.owner for mutation in MUTATIONS}) == 3
    assert {mutation.case_id for mutation in MUTATIONS} == {
        "copilot-skills-project",
        "copilot-prompt-widen-narrow",
    }


def test_wrong_target_has_a_distinct_native_destination(tmp_path: Path) -> None:
    """Shared .agents skill targets would make this mutant equivalent."""
    from apm_cli.integration.skill_integrator import SkillIntegrator
    from apm_cli.integration.targets import KNOWN_TARGETS

    original = SkillIntegrator._target_skills_root(KNOWN_TARGETS["copilot"], tmp_path)
    wrong = SkillIntegrator._target_skills_root(KNOWN_TARGETS["claude"], tmp_path)
    assert original != wrong
    assert (
        tmp_path / MUTATIONS[0].witness_path == original / "skills-copilot-skills-project/SKILL.md"
    )
    assert wrong == tmp_path / ".claude/skills"


def _result(mutation: LifecycleMutation = MUTATIONS[0]) -> dict[str, Any]:
    return {
        **classify_mutation(
            mutation,
            _observation(mutation),
            _observation(mutation, mutant=True, outcome="assertion"),
        ),
        **dict.fromkeys(ISOLATION_CHECKS, True),
    }


def _junit(tmp_path: Path, report: dict[str, Any], *, pytest_status: str | None = None) -> Path:
    root = ET.Element("testsuite")
    case = ET.SubElement(root, "testcase", classname="campaign", name="mutation")
    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", name="lifecycle_mutation", value=json.dumps(report))
    if pytest_status is not None:
        ET.SubElement(case, pytest_status)
    path = tmp_path / "junit.xml"
    ET.ElementTree(root).write(path)
    return path


def test_cleanup_reach_requires_an_existing_deletion_candidate() -> None:
    mutation = MUTATIONS[2]
    mutant = _observation(mutation, mutant=True, outcome="assertion")
    mutant = replace(
        mutant,
        events=(mutant.events[0], {**mutant.events[1], "existed": False}, mutant.events[2]),
    )
    result = classify_mutation(mutation, _observation(mutation), mutant)
    assert result["reached"] is False
    assert result["status"] == "error"


@pytest.mark.parametrize("pytest_status", (None, "failure", "error", "skipped"))
def test_pytest_result_vetoes_a_recorded_mutation_kill(
    tmp_path: Path, pytest_status: str | None
) -> None:
    path = _junit(tmp_path, mutation_report((_result(),)), pytest_status=pytest_status)
    results = read_mutation_results(path)
    assert results[0]["status"] == ("killed" if pytest_status is None else "error")
    assert results[0]["pytest_outcome"] == (pytest_status or "passed")
    assert results[0]["rejection_reason"] == (
        "" if pytest_status is None else f"pytest-{pytest_status}: no JUnit message"
    )


@pytest.mark.parametrize("field", ISOLATION_CHECKS)
def test_isolation_failure_cannot_earn_a_kill(tmp_path: Path, field: str) -> None:
    result = {**_result(), field: False}
    observed = read_mutation_results(_junit(tmp_path, mutation_report((result,))))
    assert observed[0]["status"] == "error"
    assert observed[0][field] is False
    assert observed[0]["rejection_reason"] == f"isolation-failed: {field}"


@pytest.mark.parametrize("pytest_status", ("failure", "error", "skipped"))
def test_rejection_diagnostics_survive_veto_report_and_completeness(
    tmp_path: Path, pytest_status: str
) -> None:
    """Keep the classifier's reason and exception alongside both independent vetoes."""
    mutant = _observation(mutant=True, outcome="assertion")
    mutant = replace(
        mutant, events=(mutant.events[0], {**mutant.events[1], "written": []}, mutant.events[2])
    )
    result = {
        **classify_mutation(MUTATIONS[0], _observation(), mutant),
        **dict.fromkeys(ISOLATION_CHECKS, True),
        "parent_catalog_unchanged": False,
    }
    original_reason = result["rejection_reason"]
    path = _junit(tmp_path, mutation_report((result,)), pytest_status=pytest_status)
    root = ET.parse(path).getroot()  # noqa: S314 - This test's own synthetic JUnit.
    node = root.find(f"./testcase/{pytest_status}")
    assert node is not None
    node.set("message", "pytest veto message")
    node.text = "pytest veto detail"
    ET.ElementTree(root).write(path)
    observed = read_mutation_results(path)[0]
    reason = (
        f"{original_reason}; pytest-{pytest_status}: pytest veto message; pytest veto detail; "
        "isolation-failed: parent_catalog_unchanged"
    )
    assert observed["status"] == "error"
    assert observed["rejection_reason"] == reason
    assert observed["failure_diagnostic"] == mutant.diagnostic
    assert observed["artifact_path"] == path.as_posix()
    assert observed["testcase"] == "campaign::mutation"
    siblings = [{**_result(mutation), "pytest_outcome": "passed"} for mutation in MUTATIONS[1:]]
    report_path = tmp_path / "mutations.json"
    report = write_report(report_path, (observed, *siblings))
    assert report["results"] == json.loads(report_path.read_text(encoding="ascii"))["results"]
    persisted = next(row for row in report["results"] if row["mutant_id"] == MUTATIONS[0].id)
    assert persisted["rejection_reason"] == reason
    assert persisted["failure_diagnostic"] == mutant.diagnostic
    with pytest.raises(AssertionError) as error:
        assert_complete_mutation_evidence((observed, *siblings))
    assert reason in str(error.value)
    assert path.as_posix() in str(error.value)
    assert "campaign::mutation" in str(error.value)


def test_readback_recomputes_rejection_reason_from_observations(tmp_path: Path) -> None:
    """An error's plausible but unrelated explanation is not an observed fact."""
    mutant = replace(
        _observation(mutant=True, outcome="assertion"), failure_function="unrelated_assertion"
    )
    result = {
        **classify_mutation(MUTATIONS[0], _observation(), mutant),
        **dict.fromkeys(ISOLATION_CHECKS, True),
    }
    report = mutation_report((result,))
    path = _junit(tmp_path, report)
    observed = read_mutation_results(path)[0]
    assert observed["status"] == "error"
    assert observed["rejection_reason"].startswith("unintended-oracle-failure:")
    report["results"][0]["rejection_reason"] = "effect-not-observed: mutant"
    with pytest.raises(ValueError, match="disagrees with observed rejection_reason"):
        read_mutation_results(_junit(tmp_path, report))


@pytest.mark.parametrize("pytest_status", (None, "failure", "error", "skipped"))
@pytest.mark.parametrize("isolation", (True, False))
def test_path_wrapper_and_root_extractor_are_equivalent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pytest_status: str | None, isolation: bool
) -> None:
    """Extraction is read-only and does no parse; the compatibility wrapper does one."""
    result = {**_result(), "parent_owners_unchanged": isolation}
    path = _junit(tmp_path, mutation_report((result,)), pytest_status=pytest_status)
    root = ET.parse(path).getroot()  # noqa: S314 - This test's own synthetic JUnit.
    before = ET.tostring(root)
    parse = ET.parse
    parsed_paths = []

    def counted_parse(path: Path) -> ET.ElementTree:
        parsed_paths.append(path)
        return parse(path)

    monkeypatch.setattr(ET, "parse", counted_parse)
    extracted = read_mutation_results_from_root(root, path=path)
    assert parsed_paths == []
    wrapped = read_mutation_results(path)
    assert parsed_paths == [path]
    assert wrapped == extracted
    assert ET.tostring(root) == before


@pytest.mark.parametrize(
    "fault",
    (
        "duplicate-property",
        "missing-value",
        "bad-json",
        "nonobject-report",
        "nonlist-results",
        "empty-results",
        "duplicate-results",
        "nonobject-result",
        "unknown-mutant",
        "invalid-status",
        "invalid-observation",
        "invalid-isolation",
    ),
)
def test_path_and_root_readers_reject_same_malformed_evidence(tmp_path: Path, fault: str) -> None:
    """Sharing a parse must not lose ambiguity, type, count or isolation guards."""
    path = _junit(tmp_path, mutation_report((_result(),)))
    root = ET.parse(path).getroot()  # noqa: S314 - This test's own synthetic JUnit.
    properties = root.find("./testcase/properties")
    assert properties is not None
    prop = properties.find("property")
    assert prop is not None
    report = json.loads(prop.attrib["value"])
    if fault == "duplicate-property":
        ET.SubElement(properties, "property", **prop.attrib)
    elif fault == "missing-value":
        prop.attrib.pop("value")
    elif fault == "bad-json":
        prop.set("value", "{")
    else:
        if fault == "nonobject-report":
            report = []
        elif fault == "nonlist-results":
            report["results"] = {}
        elif fault == "empty-results":
            report["results"] = []
        elif fault == "duplicate-results":
            report["results"] *= 2
        elif fault == "nonobject-result":
            report["results"] = [None]
        elif fault == "unknown-mutant":
            report["results"][0]["mutant_id"] = "unknown"
        elif fault == "invalid-status":
            report["results"][0]["status"] = True
        elif fault == "invalid-observation":
            report["results"][0]["baseline"]["events"] = [None]
        else:
            report["results"][0]["production_sources_unchanged"] = "true"
        prop.set("value", json.dumps(report))
    ET.ElementTree(root).write(path)
    with pytest.raises(ValueError) as from_path:
        read_mutation_results(path)
    with pytest.raises(ValueError) as from_root:
        read_mutation_results_from_root(root, path=path)
    assert str(from_path.value) == str(from_root.value)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("status", "survived"),
        ("owner", "unrelated.owner"),
        ("case_id", "unrelated-case"),
        ("intended_property", "unrelated-law"),
        ("reached", False),
        ("baseline_green", False),
        ("rejection_reason", "invented reason"),
        ("rejection_reason", None),
        ("failure_diagnostic", "invented diagnostic"),
        ("parent_catalog_unchanged", None),
    ),
)
def test_junit_cannot_replace_observed_facts(tmp_path: Path, field: str, value: object) -> None:
    report = mutation_report((_result(),))
    report["results"][0][field] = value
    with pytest.raises(ValueError, match=r"Mutation evidence disagrees|isolation check"):
        read_mutation_results(_junit(tmp_path, report))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", 2),
        ("schema_version", True),
        ("execution_boundary", "frozen-binary"),
        ("mutation_isolation", "parent-process"),
        ("counts", {"killed": 3, "survived": 0, "error": 0, "total": 3}),
        ("counts", {"killed": True, "survived": 0, "error": 0, "total": 1}),
    ),
)
def test_report_envelope_cannot_invent_coverage(tmp_path: Path, field: str, value: object) -> None:
    report = {**mutation_report((_result(),)), field: value}
    with pytest.raises(ValueError, match=r"Invalid mutation report|counts must be integers"):
        read_mutation_results(_junit(tmp_path, report))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("elapsed_seconds", True),
        ("elapsed_seconds", float("nan")),
        ("elapsed_seconds", -1),
        ("events", [None]),
        ("events", "not-events"),
        ("diagnostic", None),
    ),
)
def test_malformed_observations_are_rejected(tmp_path: Path, field: str, value: object) -> None:
    report = mutation_report((_result(),))
    report["results"][0]["mutant"][field] = value
    with pytest.raises(ValueError, match="Mutation"):
        read_mutation_results(_junit(tmp_path, report))


def test_nonobject_child_event_is_an_error(tmp_path: Path) -> None:
    events = tmp_path / "events.jsonl"
    events.write_text("null\n", encoding="ascii")
    observed = observe_run(lambda: None, events)
    assert observed.outcome == "error"
    assert observed.events == ()
    assert "Reach events must be objects" in observed.diagnostic


def test_complete_gate_requires_all_unique_mutants_and_passed_nodes(tmp_path: Path) -> None:
    results = [
        read_mutation_results(_junit(tmp_path, mutation_report((_result(mutation),))))[0]
        for mutation in MUTATIONS
    ]
    assert_complete_mutation_evidence(results)
    with pytest.raises(AssertionError, match="Missing lifecycle mutation evidence"):
        assert_complete_mutation_evidence(results[:-1])
    with pytest.raises(ValueError, match="duplicate"):
        mutation_report([*results, results[0]])
    with pytest.raises(ValueError, match="Unknown"):
        mutation_report([{**results[0], "mutant_id": "unknown"}])
    for change in (
        {"status": "survived"},
        {"status": "error"},
        {"pytest_outcome": "skipped"},
        {"parent_owners_unchanged": False},
    ):
        with pytest.raises(AssertionError, match="Unmet lifecycle mutation obligation"):
            assert_complete_mutation_evidence([{**results[0], **change}, *results[1:]])


def test_mutation_report_persists_before_completeness_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.utils.lifecycle_interaction_report import main

    junit = tmp_path / "empty.xml"
    ET.ElementTree(ET.Element("testsuite")).write(junit)
    output = tmp_path / "mutations.json"
    interactions = tmp_path / "interactions.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "lifecycle-report",
            "--junit",
            str(junit),
            "--revision",
            "contract-fixture",
            "--output",
            str(interactions),
            "--mutation-output",
            str(output),
            "--require-mutations",
            "--require-complete",
        ],
    )
    with pytest.raises(AssertionError, match="Missing lifecycle mutation evidence"):
        main()
    report = json.loads(output.read_text(encoding="ascii"))
    assert report["input_revision"] == "contract-fixture"
    assert report["unexecuted_mutant_ids"] == sorted(mutation.id for mutation in MUTATIONS)
    assert report["counts"] == {"killed": 0, "survived": 0, "error": 0, "total": 0}
    assert interactions.is_file()
