"""Fail-closed regressions for authored plans and fresh pytest observations."""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import pytest

from scripts.check_lifecycle_evidence import candidate, execute, main, source_profile
from scripts.lifecycle_contracts import (
    EvidenceError,
    command_inventory,
    command_path,
    runtime_candidate,
    select_contracts,
    validate_contracts,
    validate_execution,
)
from tests.utils.lifecycle_evidence import LifecycleEvidencePlugin

pytestmark = pytest.mark.component
pytest_plugins = ["pytester"]
RATCHET_TEST_SCOPE = "fixture"


def _ledger() -> dict[str, Any]:
    witnesses = [
        {
            "id": kind,
            "nodeid": f"tests/test_sample.py::test_{kind}[global]",
            "kind": kind,
            "dimensions": {"variant": "global"},
            "transitions": [
                {"command": "install", "argv_contains": [], "returncode": 0, "state": state}
                for state in ("changed", "unchanged")
            ],
        }
        for kind in ("deterministic", "generated")
    ]
    return {
        "schema_version": 1,
        "property_catalog": [{"id": "ownership.preserve_unowned"}],
        "lifecycle_contracts": [
            {
                "id": "feature",
                "assessment": "Shared global ownership affects install and repeated install.",
                "paths": ["src/new.py"],
                "properties": ["ownership.preserve_unowned"],
                "dimensions": {"variant": ["global"]},
                "commands": {
                    "install": {
                        "disposition": "applicable",
                        "reason": "Installs this feature.",
                        "deterministic": ["deterministic"],
                        "generated": ["generated"],
                    }
                },
                "witnesses": witnesses,
            }
        ],
    }


def _inventory() -> dict[str, click.Command]:
    return {"install": click.Command("install")}


def _launcher() -> Path | None:
    path = Path(sys.executable).parent / "apm"
    return path if path.is_file() else None


def _evidence() -> dict[str, Any]:
    return {
        "collected": True,
        "dimensions": {"variant": "global"},
        "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
        "models": 1,
        "events": [
            {
                "command": "install",
                "args": [],
                "returncode": 0,
                "cwd": "/fixture",
                "roots": {"HOME": "/fixture/home", "workspace": "/fixture"},
                "model": 1,
                "before": before,
                "after": after,
            }
            for before, after in (("a", "b"), ("b", "b"))
        ],
    }


@pytest.mark.parametrize(
    "path",
    [
        "src/new.py",
        "src/renamed.py",
        "unknown/feature.md",
        "packages/skill/SKILL.md",
        "install.sh",
        "uv.lock",
        "pyproject.toml",
        "templates/new.yml",
        "scripts/new.py",
    ],
)
def test_unknown_and_payload_paths_conservatively_require_assessment(path: str) -> None:
    assert runtime_candidate(path)
    empty = {**_ledger(), "lifecycle_contracts": []}
    with pytest.raises(EvidenceError, match="Missing fresh lifecycle"):
        select_contracts(empty, empty, {path}, _inventory())


def test_fresh_exact_assessment_and_renames() -> None:
    head = _ledger()
    empty = {**head, "lifecycle_contracts": []}
    assert select_contracts(empty, head, {"src/new.py"}, _inventory())
    with pytest.raises(EvidenceError, match="Missing fresh lifecycle"):
        select_contracts(head, head, {"src/new.py"}, _inventory())
    with pytest.raises(EvidenceError, match=r"src/old.py"):
        select_contracts(empty, head, {"src/new.py", "src/old.py"}, _inventory())


def test_non_runtime_exception_is_exact_reasoned_and_not_reusable() -> None:
    base = {**_ledger(), "lifecycle_contracts": []}
    head = {**base, "non_runtime_changes": [{"paths": ["scripts/tool.py"], "reason": "CI tooling"}]}
    assert select_contracts(base, head, {"scripts/tool.py"}, _inventory()) == []
    with pytest.raises(EvidenceError, match="Missing fresh"):
        select_contracts(head, head, {"scripts/tool.py"}, _inventory())
    head["non_runtime_changes"][0]["paths"] = ["scripts/*"]
    with pytest.raises(EvidenceError, match="exact repository"):
        select_contracts(base, head, {"scripts/tool.py"}, _inventory())


@pytest.mark.parametrize("mutation", ["contract", "witness", "dimension", "command", "path"])
def test_coordinated_obligation_removal_fails(mutation: str) -> None:
    base = _ledger()
    head = copy.deepcopy(base)
    row = head["lifecycle_contracts"][0]
    if mutation == "contract":
        head["lifecycle_contracts"] = []
    elif mutation == "witness":
        row["witnesses"][0]["nodeid"] = "tests/test_other.py::test_other"
    elif mutation == "dimension":
        row["dimensions"] = {}
    elif mutation == "command":
        row["commands"]["install"] = {"disposition": "semantic_na", "reason": "Removed"}
    else:
        row["paths"] = ["src/replacement.py"]
    with pytest.raises(EvidenceError):
        select_contracts(base, head, set(), _inventory())


@pytest.mark.parametrize("mutation", ["inventory", "kind", "dimension", "transition", "property"])
def test_incomplete_contract_fails(mutation: str) -> None:
    ledger = _ledger()
    row = ledger["lifecycle_contracts"][0]
    if mutation == "inventory":
        row["commands"] = {}
    elif mutation == "kind":
        row["witnesses"][1]["kind"] = "deterministic"
    elif mutation == "dimension":
        row["dimensions"]["variant"].append("aliased")
    elif mutation == "transition":
        row["witnesses"][0]["transitions"] = []
    else:
        row["properties"] = ["invented.property"]
    with pytest.raises(EvidenceError):
        validate_contracts(ledger, _inventory())


@pytest.mark.parametrize(
    "mutation",
    [
        "uncollected",
        "skip",
        "xfail",
        "setup",
        "call",
        "teardown",
        "dimension",
        "model",
        "missing-transition",
        "status",
        "workspace",
        "state",
        "json-only",
        "outside-model",
        "roots",
        "continuity",
        "model-splice",
    ],
)
def test_execution_obligations_fail_closed(mutation: str) -> None:
    witness = _ledger()["lifecycle_contracts"][0]["witnesses"][1]
    evidence = _evidence()
    validate_execution(witness, evidence)
    if mutation == "uncollected":
        evidence["collected"] = False
    elif mutation in {"setup", "call", "teardown"}:
        evidence["phases"][mutation] = "failed"
    elif mutation in {"skip", "xfail"}:
        evidence["phases"]["call"] = mutation
    elif mutation == "dimension":
        evidence["dimensions"] = {}
    elif mutation == "model":
        evidence["models"] = 0
    elif mutation == "missing-transition":
        evidence["events"].pop()
    elif mutation == "status":
        evidence["events"][1]["returncode"] = 1
    elif mutation == "workspace":
        evidence["events"][1]["cwd"] = "/other"
    elif mutation == "state":
        evidence["events"][1]["after"] = "different"
    elif mutation == "outside-model":
        evidence["events"][1]["model"] = None
    elif mutation == "roots":
        evidence["events"][1]["roots"]["HOME"] = "/other/home"
    elif mutation == "continuity":
        evidence["events"][1].update(before="different", after="different")
    elif mutation == "model-splice":
        evidence["models"] = 2
        evidence["events"][1]["model"] = 2
    else:
        evidence = {"status": "passed"}
    with pytest.raises(EvidenceError):
        validate_execution(witness, evidence)


def test_intentional_fixture_mutation_requires_reviewed_preparation() -> None:
    witness = _ledger()["lifecycle_contracts"][0]["witnesses"][1]
    evidence = _evidence()
    evidence["events"][1].update(before="tampered", after="tampered")
    with pytest.raises(EvidenceError, match="trajectory"):
        validate_execution(witness, evidence)
    witness["transitions"][1]["preparation"] = "User tampers with deployed bytes before audit."
    validate_execution(witness, evidence)


def test_inventory_uses_recursive_registrations_and_aliases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from apm_cli.cli import cli

    monkeypatch.setitem(cli.commands, "fixture-alias", cli.commands["install"])
    inventory = command_inventory()
    assert inventory["fixture-alias"] is inventory["install"]
    assert "marketplace package add" in inventory
    assert "" in inventory and "lock" in inventory and "lock export" in inventory
    assert command_path(["--verbose", "cache", "prune", "--help"], inventory) == "cache prune"
    assert command_path(["lock"], inventory) == "lock"
    assert command_path(["--help"], inventory) == ""


def test_candidate_rejects_stale_head_and_dirty_tree(tmp_path: Path) -> None:
    def run(*args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    run("init", "-q")
    run("config", "user.email", "fixture@example.invalid")
    run("config", "user.name", "Fixture")
    run("commit", "--allow-empty", "-qm", "base")
    base = run("rev-parse", "HEAD")
    run("commit", "--allow-empty", "-qm", "head")
    head = run("rev-parse", "HEAD")
    assert candidate(tmp_path, base, head)["tested_tree"] == run("rev-parse", "HEAD^{tree}")
    with pytest.raises(EvidenceError, match="checked-out"):
        candidate(tmp_path, base, base)
    (tmp_path / "unknown").write_text("dirty")
    with pytest.raises(EvidenceError, match="clean"):
        candidate(tmp_path, base, head)


def test_plugin_observes_actual_execution_and_rejects_deselection(
    pytester: pytest.Pytester,
) -> None:
    module = pytester.makepyfile("""
        import os
        import sys
        import pytest
        from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
        from hypothesis import settings
        from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test
        @pytest.mark.parametrize("variant", ["global"])
        def test_real(tmp_path, variant):
            env = {**os.environ, "HOME": str(tmp_path / "home")}
            runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
            for _ in range(2):
                result = runner.run(["--version"], cwd=tmp_path, env=env)
                assert result.returncode == 0
        def test_skip():
            pytest.skip("contract skip")
        @pytest.mark.xfail(reason="contract xfail")
        def test_xfail():
            assert False
        @pytest.mark.parametrize("variant", ["global"])
        def test_generated(tmp_path, variant):
            class Model(RuleBasedStateMachine):
                @rule()
                def version(self):
                    env = {**os.environ, "HOME": str(tmp_path / "home")}
                    runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
                    for _ in range(2):
                        assert runner.run(["--version"], cwd=tmp_path, env=env).returncode == 0
            run_state_machine_as_test(
                Model, settings=settings(
                    max_examples=1, stateful_step_count=1, deadline=None, database=None
                )
            )
    """)
    path = module.name
    real = f"{path}::test_real[global]"
    skipped, xfailed = f"{path}::test_skip", f"{path}::test_xfail"
    generated = f"{path}::test_generated[global]"
    executable = _launcher()
    plugin = LifecycleEvidencePlugin([real, skipped, xfailed, generated], executable)
    result = pytester.runpytest_inprocess("-q", "-o", "addopts=", plugins=[plugin])
    result.assert_outcomes(passed=2, skipped=1, xfailed=1)
    record = plugin.records[real]
    assert record["dimensions"] == {"variant": "global"}
    assert len(record["events"]) == 2
    assert record["phases"] == {"setup": "passed", "call": "passed", "teardown": "passed"}
    assert plugin.records[skipped]["phases"]["call"] == "skipped"
    assert plugin.records[xfailed]["phases"]["call"] == "xfail"
    assert plugin.records[generated]["models"] == 1
    assert len(plugin.records[generated]["events"]) >= 2
    assert all(event["model"] == 1 for event in plugin.records[generated]["events"])
    deselected = LifecycleEvidencePlugin([real], executable)
    result = pytester.runpytest_inprocess("-q", "-k", "skip", plugins=[deselected])
    assert real not in deselected.records
    assert result.ret == 0
    assert json.dumps(record)


@pytest.mark.parametrize("phase", ["setup", "teardown"])
def test_plugin_records_fixture_failures(pytester: pytest.Pytester, phase: str) -> None:
    module = pytester.makepyfile(f"""
        import pytest
        @pytest.fixture
        def broken():
            if {phase!r} == "setup":
                raise RuntimeError("fixture failed")
            yield
            raise RuntimeError("fixture failed")
        def test_fixture(broken):
            assert True
    """)
    nodeid = f"{module.name}::test_fixture"
    plugin = LifecycleEvidencePlugin([nodeid], _launcher())
    result = pytester.runpytest_inprocess("-q", plugins=[plugin])
    assert result.ret != 0
    assert plugin.records[nodeid]["phases"][phase] == "failed"


@pytest.mark.parametrize("selector", ["::WrongClass::test_exists", "::test_exists[missing]"])
def test_plugin_rejects_wrong_class_or_parameter(pytester: pytest.Pytester, selector: str) -> None:
    module = pytester.makepyfile("""
        import pytest
        @pytest.mark.parametrize("variant", ["actual"])
        def test_exists(variant):
            assert variant
    """)
    nodeid = f"{module.name}{selector}"
    plugin = LifecycleEvidencePlugin([nodeid], _launcher())
    result = pytester.runpytest_inprocess("-q", nodeid, plugins=[plugin])
    assert result.ret != 0
    assert not plugin.records


@pytest.mark.skipif(os.name == "nt", reason="Unix script launcher; Windows uses the source module")
def test_source_profile_rejects_foreign_source_and_stale_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import apm_cli

    monkeypatch.setattr(apm_cli, "__file__", str(tmp_path / "src/apm_cli/__init__.py"))
    python = tmp_path / "python"
    python.write_text("fixture interpreter", encoding="ascii")
    monkeypatch.setattr(sys, "executable", str(python))
    assert source_profile(tmp_path)[0] is None
    launcher = tmp_path / "apm"
    launcher.write_text("#!/foreign/python\nfrom apm_cli.cli import cli\n", encoding="ascii")
    with pytest.raises(EvidenceError, match="this Python environment"):
        source_profile(tmp_path)
    launcher.write_text(f"#!{python}\nfrom apm_cli.cli import cli\n", encoding="ascii")
    _, initial = source_profile(tmp_path)
    launcher.write_text(f"#!{python}\nfrom apm_cli.cli import cli\n# changed\n", encoding="ascii")
    assert source_profile(tmp_path)[1] != initial
    with pytest.raises(EvidenceError, match="candidate checkout"):
        source_profile(tmp_path / "different")


def test_cli_rejects_in_tree_receipts(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.check_lifecycle_evidence import ROOT

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check_lifecycle_evidence.py",
            "--base",
            "a",
            "--head",
            "b",
            "--lane",
            "full",
            "--report",
            str(ROOT / "receipt.json"),
        ],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2


@pytest.mark.parametrize("lane,status", [("pr", "pending"), ("full", "passed")])
def test_execute_produces_candidate_bound_native_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lane: str, status: str
) -> None:
    """Exercise the gate, git comparison, real collection, subprocesses and model together."""
    from scripts import check_lifecycle_evidence as gate

    profile = source_profile(gate.ROOT)
    monkeypatch.setattr(gate, "ROOT", tmp_path)
    monkeypatch.setattr(gate, "source_profile", lambda _root: profile)
    monkeypatch.chdir(tmp_path)
    tests = tmp_path / "tests"
    tests.mkdir()
    filename = f"test_native_probe_{lane}.py"
    (tests / filename).write_text(
        """
import os
import sys
import pytest
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, rule, run_state_machine_as_test
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
@pytest.mark.parametrize("kind", ["deterministic", "generated"])
def test_probe(tmp_path, kind):
    def commands():
        env = {**os.environ, "HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")}
        runner = ApmLifecycleRunner((sys.executable, "-m", "apm_cli.cli"))
        for _ in range(2):
            assert runner.run(["--version"], cwd=tmp_path, env=env).returncode == 0
    if kind == "deterministic":
        commands()
    else:
        class Model(RuleBasedStateMachine):
            @rule()
            def version(self):
                commands()
        run_state_machine_as_test(Model, settings=settings(
            max_examples=1, stateful_step_count=1, database=None, deadline=None))
""",
        encoding="ascii",
    )
    ledger = _ledger()
    path = tests / "fixtures/lifecycle_bug_ledger.json"
    path.parent.mkdir()
    path.write_text(json.dumps({**ledger, "lifecycle_contracts": []}), encoding="ascii")

    def commit(message: str) -> str:
        subprocess.run(["git", "add", "."], check=True)
        subprocess.run(["git", "commit", "-qm", message], check=True)
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.invalid"], check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], check=True)
    (tmp_path / ".gitignore").write_text("__pycache__/\n.hypothesis/\n", encoding="ascii")
    base = commit("base")
    contract = ledger["lifecycle_contracts"][0]
    contract["dimensions"] = {}
    contract["commands"] = {
        name: {"disposition": "semantic_na", "reason": "Fixture only tests version reporting."}
        for name in command_inventory()
    }
    contract["commands"][""] = {
        "disposition": "applicable",
        "reason": "Version reporting fixture.",
        "deterministic": ["deterministic"],
        "generated": ["generated"],
    }
    for witness in contract["witnesses"]:
        witness["nodeid"] = f"tests/{filename}::test_probe[{witness['kind']}]"
        witness["dimensions"] = {"kind": witness["kind"]}
        for transition in witness["transitions"]:
            transition.update(command="", argv_contains=["--version"], state="observed")
    path.write_text(json.dumps(ledger), encoding="ascii")
    head = commit("contract")
    report = execute(argparse.Namespace(base=base, head=head, lane=lane))
    assert report["status"] == status, report
    assert report["base"] == base and report["head"] == head
    assert len(report["witnesses"]) == (2 if lane == "full" else 1)
    assert all(value["events"] for value in report["witnesses"].values())
