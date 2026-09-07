"""Fast executable model/oracle contracts; no binary, network, or E2E prerequisite."""

from __future__ import annotations

import ast
import json
import shutil
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from tests.utils import lifecycle_model_driver
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.lifecycle_model import (
    CORPUS_PATH,
    FAILED_COMMAND,
    IDEMPOTENCY,
    LAWS,
    OPEN_WORLD,
    OUTCOME,
    OWNERSHIP,
    READ_ONLY,
    ROUTING,
    SOURCE,
    TRANSITIONS,
    LifecycleModel,
    ReplayCase,
    advance,
    applicable_laws,
    legal_transitions,
    load_corpus,
)
from tests.utils.lifecycle_model_driver import (
    CACHE_ROOT,
    CACHE_SKILL_PATH,
    LAW_CHECKS,
    SENTINELS,
    SKILL_BYTES,
    SKILL_PATH,
    SOURCE_ROOT,
    SOURCE_SKILL_PATH,
    LifecycleDriver,
    TransitionObservation,
    observe,
)

pytestmark = [pytest.mark.component, pytest.mark.lifecycle_smoke]

_REVISION = "a" * 40
_DEPENDENCY = {"git": "fixture/model-kit", "ref": _REVISION, "alias": "model-kit"}
_EXPECTED_TRANSITIONS = {
    "install",
    "reinstall",
    "dry_run",
    "tamper",
    "audit_tampered",
    "repair",
    "audit_clean",
    "remove_declaration",
    "readd_declaration",
    "prune_removed",
}
_STATE_CASES = (
    (LifecycleModel(), {"install", "dry_run", "remove_declaration"}),
    (LifecycleModel(False), {"readd_declaration"}),
    (
        LifecycleModel(True, True),
        {"reinstall", "dry_run", "tamper", "audit_clean", "remove_declaration"},
    ),
    (
        LifecycleModel(True, True, False),
        {"dry_run", "audit_tampered", "repair", "remove_declaration"},
    ),
    (LifecycleModel(False, True), {"tamper", "readd_declaration", "prune_removed"}),
    (LifecycleModel(False, True, False), {"readd_declaration", "prune_removed"}),
)
_LAW_TRANSITIONS = {
    OPEN_WORLD: "install",
    OWNERSHIP: "install",
    ROUTING: "install",
    SOURCE: "install",
    OUTCOME: "install",
    IDEMPOTENCY: "reinstall",
    FAILED_COMMAND: "audit_tampered",
    READ_ONLY: "dry_run",
}
_REVIEWED_DESIGNED_REPLAYS = {
    "designed-declaration-roundtrip-before-install": (
        "remove_declaration",
        "readd_declaration",
        "dry_run",
        "install",
        "audit_clean",
    ),
    "designed-tampered-declaration-roundtrip": (
        "install",
        "tamper",
        "remove_declaration",
        "readd_declaration",
        "audit_tampered",
        "repair",
        "reinstall",
    ),
}


def _assert_reviewed_designed_replays(cases: tuple[ReplayCase, ...]) -> None:
    """Pin the two reviewed designs without closing the generic corpus format."""
    actual = {case.case_id: case.sequence for case in cases[1:]}
    assert _REVIEWED_DESIGNED_REPLAYS.items() <= actual.items(), (
        "Reviewed designed replay obligations changed"
    )


def _write(root: Path, path: str, content: bytes) -> None:
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(content)


def _completed_audit_payload(clean: bool, path: str = SKILL_PATH) -> dict[str, object]:
    """Author complete fake receipts; the shared validator alone adjudicates them."""
    checks = [
        {"name": name, "passed": True, "message": "ok", "details": []}
        for name in (
            "lockfile-exists",
            "ref-consistency",
            "deployment-ledger-owners",
            "deployed-files-present",
            "no-orphaned-packages",
            "skill-subset-consistency",
            "config-consistency",
            "content-integrity",
            "includes-consent",
        )
    ]
    if not clean:
        integrity = next(check for check in checks if check["name"] == "content-integrity")
        integrity.update(
            passed=False,
            message="content hash mismatch",
            details=[f"hash-drift: {path} (dep=fixture/model-kit)"],
        )
    checks.append(
        {
            "name": "drift",
            "passed": clean,
            "message": "no drift detected against lockfile" if clean else "1 drift finding",
            "details": [] if clean else [f"modified: {path}"],
        }
    )
    failed = 0 if clean else 2
    return {
        "passed": clean,
        "checks": checks,
        "summary": {"total": len(checks), "passed": len(checks) - failed, "failed": failed},
        "drift": {"drift": [] if clean else [{"kind": "modified", "path": path}]},
    }


def _component_driver(root: Path, identity: str) -> tuple[LifecycleDriver, list[CommandResult]]:
    """An explicit filesystem fake validates the harness, not production behavior."""
    roots = {"project": root / "project", "user": root / "user"}
    for path in roots.values():
        path.mkdir(parents=True)

    def declare(enabled: bool) -> None:
        manifest = {"dependencies": {"apm": [_DEPENDENCY] if enabled else []}}
        _write(roots["project"], "apm.yml", json.dumps(manifest).encode("ascii"))

    declare(True)
    for root_id, path, content in SENTINELS:
        _write(roots[root_id], path, content)
    results = []

    def run(args: tuple[str, ...], case_id: str) -> CommandResult:
        assert case_id == identity
        project = roots["project"]
        code, stdout = 0, ""
        if args[0] == "install":
            if "--dry-run" in args:
                stdout = "[i] APM dependencies (1):\n"
            else:
                _write(project, SKILL_PATH, SKILL_BYTES)
                _write(project, CACHE_SKILL_PATH, SKILL_BYTES)
                _write(project, SOURCE_SKILL_PATH, SKILL_BYTES)
                _write(
                    project,
                    f"{SOURCE_ROOT}/.apm-pin",
                    json.dumps({"resolved_commit": _REVISION}).encode("ascii"),
                )
                _write(
                    project,
                    "apm.lock.yaml",
                    json.dumps({"dependencies": [{"resolved_commit": _REVISION}]}).encode("ascii"),
                )
        elif args[0] == "audit":
            clean = (project / SKILL_PATH).read_bytes() == SKILL_BYTES
            code = 0 if clean else 1
            stdout = json.dumps(_completed_audit_payload(clean))
        elif args[0] == "prune":
            shutil.rmtree(project / CACHE_ROOT)
            shutil.rmtree(project / SOURCE_ROOT)
            shutil.rmtree((project / SKILL_PATH).parent)
            (project / "apm.lock.yaml").unlink()
        else:
            raise AssertionError(f"Unknown fake command: {args}")
        result = CommandResult(("fake-apm", *args), code, stdout, "", project)
        results.append(result)
        return result

    driver = LifecycleDriver(
        roots=roots,
        case_id=identity,
        dependency=_DEPENDENCY,
        source_revision=_REVISION,
        run_command=run,
        set_declared=declare,
    )
    return driver, results


def _valid_observation(
    root: Path, transition: str
) -> tuple[LifecycleDriver, TransitionObservation]:
    driver, results = _component_driver(root, f"corruption-{transition}")
    if transition in {"reinstall", "audit_tampered"}:
        driver.apply("install")
    if transition == "audit_tampered":
        driver.apply("tamper")
    before = observe(driver.roots)
    driver.apply(transition)
    return driver, TransitionObservation(
        before,
        observe(driver.roots),
        driver.state,
        transition,
        results[-1],
        driver.dependency,
        _REVISION,
    )


def test_law_and_transition_denominators_are_explicit_and_nonempty() -> None:
    """Deleting an obligation from both a catalog and its implementation is not coverage."""
    assert set(TRANSITIONS) == _EXPECTED_TRANSITIONS
    assert len(TRANSITIONS) == 10
    assert set(LAWS) == {
        "filesystem.open_world_observation",
        "ownership.preserve_unowned",
        "routing.authorized_targets_only",
        "source.ref_cache_coherent",
        "outcome.status_matches_state",
        "idempotency.byte_stable",
        "transaction.failed_command_preserves_state",
        "observation.read_only_preserves_state",
    }
    assert set(LAW_CHECKS) == LAWS == set(_LAW_TRANSITIONS)


@pytest.mark.parametrize(("state", "enabled"), _STATE_CASES)
@pytest.mark.parametrize("transition", (*TRANSITIONS, "unknown"))
def test_transition_function_is_total_over_reachable_states(
    state: LifecycleModel, enabled: set[str], transition: str
) -> None:
    assert set(legal_transitions(state)) == enabled
    if transition not in enabled:
        with pytest.raises(ValueError, match="Illegal transition"):
            advance(state, transition)
        with pytest.raises(ValueError, match="Illegal transition"):
            applicable_laws(state, transition)
        return
    after = advance(state, transition)
    fields = (state.declared, state.materialized, state.clean)
    if transition in {"install", "repair"}:
        fields = (state.declared, True, True)
    elif transition == "tamper":
        fields = (state.declared, state.materialized, False)
    elif transition in {"remove_declaration", "readd_declaration"}:
        fields = (transition == "readd_declaration", state.materialized, state.clean)
    elif transition == "prune_removed":
        fields = (False, False, True)
    assert after == LifecycleModel(*fields)
    assert after in {candidate for candidate, _ in _STATE_CASES}
    assert applicable_laws(state, transition) <= LAWS
    assert {OPEN_WORLD, OUTCOME, ROUTING, OWNERSHIP} <= applicable_laws(state, transition)


def test_model_is_immutable_and_all_valid_states_are_reachable() -> None:
    initial = LifecycleModel()
    with pytest.raises(FrozenInstanceError):
        initial.declared = False
    reached = {initial}
    pending = [initial]
    while pending:
        state = pending.pop()
        for transition in legal_transitions(state):
            successor = advance(state, transition)
            if successor not in reached:
                reached.add(successor)
                pending.append(successor)
    assert reached == {state for state, _ in _STATE_CASES}
    with pytest.raises(ValueError, match="Absent materialization"):
        LifecycleModel(clean=False)
    with pytest.raises(ValueError, match="must be booleans"):
        LifecycleModel(declared=1)


@pytest.mark.parametrize("case", load_corpus(), ids=lambda case: case.case_id)
def test_literal_corpus_replay_records_only_fired_actions(tmp_path: Path, case: ReplayCase) -> None:
    driver, results = _component_driver(tmp_path, case.case_id)
    assert driver.evidence()["status"] == "not_executed"
    driver.replay(case)
    evidence = driver.evidence()
    assert evidence["status"] == "passed"
    assert evidence["sequence"] == list(case.sequence)
    assert [row["transition"] for row in evidence["transitions"]] == list(case.sequence)
    state = LifecycleModel()
    for step, transition in enumerate(case.sequence, 1):
        fired = [row for row in evidence["laws"] if row["step"] == step]
        assert {row["law"] for row in fired} == applicable_laws(state, transition)
        assert all(row["passed"] for row in fired)
        state = advance(state, transition)
    assert len(results) == sum(
        transition not in {"tamper", "remove_declaration", "readd_declaration"}
        for transition in case.sequence
    )


@pytest.mark.parametrize(
    ("transition", "absolute_path"),
    (("audit_clean", False), ("audit_tampered", False), ("audit_tampered", True)),
    ids=("clean-completed", "tampered-relative", "tampered-absolute"),
)
def test_model_audit_accepts_completed_attributable_receipts(
    tmp_path: Path, transition: str, absolute_path: bool
) -> None:
    """A completed audit earns credit using fixture roots, not the receipt's cwd."""
    driver, results = _component_driver(tmp_path, "completed-audit")
    driver.apply("install")
    if transition == "audit_tampered":
        driver.apply("tamper")
    original = driver.run_command
    clean = transition == "audit_clean"

    def completed_run(args: tuple[str, ...], identity: str) -> CommandResult:
        result = original(args, identity)
        path = (driver.roots["project"] / SKILL_PATH).as_posix() if absolute_path else SKILL_PATH
        return replace(
            result,
            stdout=json.dumps(_completed_audit_payload(clean, path)),
            cwd=tmp_path / "untrusted-receipt-root",
        )

    driver.run_command = completed_run
    before = observe(driver.roots)
    driver.apply(transition)
    assert observe(driver.roots) == before
    assert results[-1].command == (
        "fake-apm",
        "audit",
        "--ci",
        "--no-policy",
        "--no-fail-fast",
        "--format",
        "json",
    )
    evidence = driver.evidence()
    assert evidence["status"] == "passed"
    assert evidence["failure"] is None
    audit_laws = [row for row in evidence["laws"] if row["step"] == len(driver.sequence)]
    assert {row["law"] for row in audit_laws} == applicable_laws(driver.state, transition)
    assert all(row["passed"] for row in audit_laws)
    assert next(row for row in audit_laws if row["law"] == OUTCOME)["passed"] is True


@pytest.mark.parametrize("transition", ("audit_clean", "audit_tampered"))
@pytest.mark.parametrize(
    ("corruption", "diagnostic"),
    (
        ("status-only", "missing completed checks"),
        ("opposite-state", "exit="),
        ("skipped-replay", "replay"),
        ("incomplete-inventory", "incomplete verification"),
        ("incomplete-replay", "missing completed replay findings"),
        ("unrelated-check", "unrelated failed checks"),
        ("unrelated-path", "unrelated or incomplete integrity finding"),
    ),
)
def test_model_audit_rejects_unattributed_receipts(
    tmp_path: Path, transition: str, corruption: str, diagnostic: str
) -> None:
    """Invalid receipts fail the actual driver outcome law before it earns credit."""
    driver, _ = _component_driver(tmp_path, f"{transition}-{corruption}")
    driver.apply("install")
    if transition == "audit_tampered":
        driver.apply("tamper")
    # Every negative follows a completed positive through the same command boundary.
    driver.apply(transition)
    assert driver.evidence()["status"] == "passed"
    original = driver.run_command
    clean = transition == "audit_clean"

    def invalid_run(args: tuple[str, ...], identity: str) -> CommandResult:
        result = original(args, identity)
        payload = json.loads(result.stdout)
        checks = {check["name"]: check for check in payload["checks"]}
        if corruption == "status-only":
            payload = {"passed": clean}
        elif corruption == "opposite-state":
            payload = _completed_audit_payload(not clean)
            result = replace(result, returncode=1 if clean else 0)
        elif corruption == "skipped-replay":
            checks["drift"].update(passed=True, message="drift check skipped", details=[])
            payload["drift"] = {"drift": []}
        elif corruption == "incomplete-inventory":
            payload["checks"].remove(checks["includes-consent"])
        elif corruption == "incomplete-replay":
            del payload["drift"]
        elif corruption == "unrelated-check":
            payload = _completed_audit_payload(True)
            payload["passed"] = clean
            unrelated = next(
                check for check in payload["checks"] if check["name"] == "ref-consistency"
            )
            unrelated.update(passed=False, message="unrelated ref failure")
        elif clean:
            # The clean counterpart must reject unexpected replay findings, too.
            payload["drift"]["drift"] = [{"kind": "modified", "path": "unrelated.md"}]
        else:
            payload = _completed_audit_payload(False, "unrelated.md")
        if "checks" in payload:
            failed = sum(not check["passed"] for check in payload["checks"])
            payload["summary"] = {
                "total": len(payload["checks"]),
                "passed": len(payload["checks"]) - failed,
                "failed": failed,
            }
        return replace(result, stdout=json.dumps(payload))

    driver.run_command = invalid_run
    before = observe(driver.roots)
    state = driver.state
    expected_error = (
        "clean receipt contains drift" if clean and corruption == "unrelated-path" else diagnostic
    )
    with pytest.raises(AssertionError, match=f"audit:.*{expected_error}") as failure:
        driver.apply(transition)
    assert observe(driver.roots) == before
    assert driver.state == state
    evidence = driver.evidence()
    assert evidence["status"] == "failed"
    assert evidence["failure"] == str(failure.value)
    report = json.loads(evidence["failure"])
    assert report["case_id"] == driver.case_id
    assert report["law"] == OUTCOME
    assert report["sequence"] == driver.sequence
    receipt_clean = not clean if corruption == "opposite-state" else clean
    assert report["returncode"] == (0 if receipt_clean else 1)
    assert json.loads(report["stdout"])["passed"] is receipt_clean
    if corruption == "opposite-state":
        assert report["error"].startswith(f"audit: exit={report['returncode']}\n")
        assert json.loads(report["stdout"]) == _completed_audit_payload(not clean)
    assert expected_error in report["error"]
    step = len(driver.sequence)
    assert evidence["transitions"][-1] == {"step": step, "transition": transition}
    assert [row for row in evidence["laws"] if row["step"] == step and row["law"] == OUTCOME] == [
        {"step": step, "transition": transition, "law": OUTCOME, "passed": False}
    ]


def test_model_audit_consumer_supplies_authored_attribution_and_complete_inventory() -> None:
    """Guard the real consumer arguments, not just the presence of a validator call."""
    module = ast.parse(Path(lifecycle_model_driver.__file__).read_text(encoding="utf-8"))
    outcome = next(
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "law_outcome"
    )
    calls = [
        node
        for node in ast.walk(outcome)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assert_ci_audit_result"
    ]
    assert len(calls) == 1, "Model audits must use the one canonical receipt validator"
    call = calls[0]
    assert [ast.dump(argument) for argument in call.args] == [
        ast.dump(ast.parse("result", mode="eval").body)
    ]
    keywords = {keyword.arg: keyword.value for keyword in call.keywords}
    expected_keywords = {
        "clean": "clean",
        "deployment_root": "project_root",
        "tampered_path": "None if clean else project_root / SKILL_PATH",
    }
    assert len(call.keywords) == len(expected_keywords)
    assert set(keywords) == set(expected_keywords), "Missing strict audit consumer arguments"
    for name, expression in expected_keywords.items():
        assert ast.dump(keywords[name]) == ast.dump(ast.parse(expression, mode="eval").body), name
    for name, expression in {
        "project_root": 'observation.before.artifacts.snapshot("project").root',
        "clean": 'observation.transition == "audit_clean"',
    }.items():
        assignments = [
            node.value
            for node in ast.walk(outcome)
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
        ]
        assert len(assignments) == 1, f"Missing unique authored audit input: {name}"
        assert ast.dump(assignments[0]) == ast.dump(ast.parse(expression, mode="eval").body), name
    assignments = {
        target.id: node.value
        for node in module.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert ast.literal_eval(assignments["AUDIT_ARGS"]) == (
        "audit",
        "--ci",
        "--no-policy",
        "--no-fail-fast",
        "--format",
        "json",
    )
    commands = assignments["_COMMANDS"]
    assert isinstance(commands, ast.Dict)
    for transition in ("audit_clean", "audit_tampered"):
        arguments = [
            value
            for key, value in zip(commands.keys, commands.values, strict=True)
            if isinstance(key, ast.Constant) and key.value == transition
        ]
        assert len(arguments) == 1
        assert isinstance(arguments[0], ast.Name) and arguments[0].id == "AUDIT_ARGS"


@pytest.mark.parametrize("law", sorted(LAWS))
def test_every_law_rejects_a_real_observed_corruption(tmp_path: Path, law: str) -> None:
    """Positive controls precede counterexamples so an always-failing law cannot pass."""
    driver, valid = _valid_observation(tmp_path, _LAW_TRANSITIONS[law])
    LAW_CHECKS[law](valid)
    if law == OUTCOME:
        corrupted = replace(valid, result=replace(valid.result, returncode=1))
    else:
        paths = {
            OPEN_WORLD: ("project", ".github/workflows/leak.yml"),
            OWNERSHIP: ("user", ".apm/unrelated.txt"),
            ROUTING: ("project", ".claude/skills/model-skill/SKILL.md"),
            SOURCE: ("project", CACHE_SKILL_PATH),
            IDEMPOTENCY: ("project", ".gitignore"),
            FAILED_COMMAND: ("project", "failed-audit-write"),
            READ_ONLY: ("user", "dry-run-write"),
        }
        root_id, path = paths[law]
        _write(driver.roots[root_id], path, SKILL_BYTES if law == ROUTING else b"corruption\n")
        corrupted = replace(valid, after=observe(driver.roots))
    with pytest.raises(AssertionError):
        LAW_CHECKS[law](corrupted)


@pytest.mark.parametrize(
    "corruption", ("missing-deployment", "stale-revision", "stale-cache-identity", "false-audit")
)
def test_semantic_laws_reject_false_green_observations(tmp_path: Path, corruption: str) -> None:
    transition = "audit_tampered" if corruption == "false-audit" else "install"
    driver, valid = _valid_observation(tmp_path, transition)
    law = SOURCE if corruption in {"stale-revision", "stale-cache-identity"} else OUTCOME
    LAW_CHECKS[law](valid)
    if corruption == "missing-deployment":
        (driver.roots["project"] / SKILL_PATH).unlink()
        invalid = replace(valid, after=observe(driver.roots))
    elif corruption == "stale-revision":
        _write(
            driver.roots["project"],
            "apm.lock.yaml",
            json.dumps({"dependencies": [{"resolved_commit": "b" * 40}]}).encode("ascii"),
        )
        invalid = replace(valid, after=observe(driver.roots))
    elif corruption == "stale-cache-identity":
        _write(
            driver.roots["project"],
            f"{SOURCE_ROOT}/.apm-pin",
            json.dumps({"resolved_commit": "b" * 40}).encode("ascii"),
        )
        invalid = replace(valid, after=observe(driver.roots))
    else:
        invalid = replace(valid, result=replace(valid.result, stdout='{"passed":true}'))
    with pytest.raises(AssertionError):
        LAW_CHECKS[law](invalid)


def test_driver_failure_has_replay_and_law_identity_without_success_credit(
    tmp_path: Path,
) -> None:
    driver, _ = _component_driver(tmp_path, "bad-install")
    original = driver.run_command

    def leaking_run(args: tuple[str, ...], identity: str) -> CommandResult:
        result = original(args, identity)
        _write(driver.roots["user"], ".apm/unrelated.txt", b"destroyed\n")
        return result

    driver.run_command = leaking_run
    with pytest.raises(AssertionError) as failure:
        driver.apply("install")
    report = json.loads(str(failure.value))
    assert report["case_id"] == "bad-install"
    assert report["sequence"] == ["install"]
    assert report["law"] == OPEN_WORLD
    evidence = driver.evidence()
    assert evidence["status"] == "failed"
    assert evidence["transitions"] == [{"step": 1, "transition": "install"}]
    assert evidence["laws"] == [
        {"step": 1, "transition": "install", "law": OPEN_WORLD, "passed": False}
    ]
    assert driver.state == LifecycleModel()


@pytest.mark.parametrize("law", sorted(LAWS))
def test_missing_law_is_not_credited_or_executed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, law: str
) -> None:
    driver, results = _component_driver(tmp_path, "missing-law")
    monkeypatch.delitem(LAW_CHECKS, law)
    with pytest.raises(AssertionError, match=r"accounting\.law_registry"):
        driver.apply("install")
    assert results == []
    assert driver.evidence()["transitions"] == []
    assert driver.evidence()["laws"] == []
    assert driver.evidence()["status"] == "failed"


def test_illegal_action_does_not_touch_the_fixture(tmp_path: Path) -> None:
    driver, results = _component_driver(tmp_path, "illegal")
    before = observe(driver.roots)
    with pytest.raises(AssertionError, match=r"transition\.legality"):
        driver.apply("repair")
    assert observe(driver.roots) == before
    assert results == []
    assert driver.evidence()["transitions"] == []


def test_checked_in_corpus_preserves_both_designed_replays() -> None:
    _assert_reviewed_designed_replays(load_corpus())


@pytest.mark.parametrize("case_id", tuple(_REVIEWED_DESIGNED_REPLAYS))
@pytest.mark.parametrize("corruption", ("deletion", "legal-shortening"))
def test_reviewed_replay_guard_rejects_deleted_or_shortened_designs(
    tmp_path: Path, case_id: str, corruption: str
) -> None:
    _assert_reviewed_designed_replays(load_corpus())
    payload = json.loads(CORPUS_PATH.read_text(encoding="ascii"))
    if corruption == "deletion":
        payload["regressions"] = [
            case for case in payload["regressions"] if case["case_id"] != case_id
        ]
    else:
        case = next(case for case in payload["regressions"] if case["case_id"] == case_id)
        case["sequence"] = [
            step
            for step in case["sequence"]
            if step not in {"remove_declaration", "readd_declaration"}
        ]
    path = tmp_path / "changed-designs.json"
    path.write_text(json.dumps(payload), encoding="ascii")
    cases = load_corpus(path)
    with pytest.raises(AssertionError, match="Reviewed designed replay obligations changed"):
        _assert_reviewed_designed_replays(cases)


def test_generic_corpus_accepts_additional_and_alternative_replays(tmp_path: Path) -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="ascii"))
    additional = {**payload["regressions"][0], "case_id": "additional-legal-replay"}
    payload["regressions"].append(additional)
    path = tmp_path / "extensible-corpus.json"
    path.write_text(json.dumps(payload), encoding="ascii")
    cases = load_corpus(path)
    _assert_reviewed_designed_replays(cases)
    assert cases[-1].case_id == additional["case_id"]
    payload["regressions"] = [additional]
    path.write_text(json.dumps(payload), encoding="ascii")
    assert [case.case_id for case in load_corpus(path)[1:]] == [additional["case_id"]]


@pytest.mark.parametrize(
    "corruption",
    (
        "schema",
        "missing-key",
        "empty-regressions",
        "duplicate-id",
        "unknown-fixture",
        "illegal-sequence",
        "omitted-transition",
        "missing-law",
        "duplicate-law",
        "string-sequence",
    ),
)
def test_corpus_rejects_malformed_or_vacuous_claims(tmp_path: Path, corruption: str) -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="ascii"))
    tour = payload["covering_tour"]
    if corruption == "schema":
        payload["schema_version"] = True
    elif corruption == "missing-key":
        del payload["regressions"]
    elif corruption == "empty-regressions":
        payload["regressions"] = []
    elif corruption == "duplicate-id":
        payload["regressions"][0]["case_id"] = tour["case_id"]
    elif corruption == "unknown-fixture":
        tour["fixture"] = "different-scope"
    elif corruption == "illegal-sequence":
        tour["sequence"] = ["repair"]
    elif corruption == "omitted-transition":
        tour["sequence"].remove("reinstall")
        tour["expected_laws"].remove(IDEMPOTENCY)
    elif corruption == "missing-law":
        tour["expected_laws"].remove(OPEN_WORLD)
    elif corruption == "duplicate-law":
        tour["expected_laws"].append(OPEN_WORLD)
    else:
        tour["sequence"] = "install"
    path = tmp_path / "invalid-corpus.json"
    path.write_text(json.dumps(payload), encoding="ascii")
    with pytest.raises(ValueError):
        load_corpus(path)
