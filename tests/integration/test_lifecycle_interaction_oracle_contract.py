"""Fast adversarial oracle contracts; no installed binary or E2E prerequisite."""

from __future__ import annotations

import ast
import json
import os
import time
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from apm_cli.core.deployment_state import (
    DeploymentLedger,
    DeploymentLocator,
    DeploymentRecord,
    LocatorKind,
)
from apm_cli.deps.lockfile import LockedDependency, LockFile
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.path_security import PathTraversalError
from tests.utils.apm_lifecycle_runner import CommandResult
from tests.utils.artifact_snapshot import ArtifactSnapshotSet, assert_snapshot_set_unchanged
from tests.utils.lifecycle_interaction_oracle import (
    InteractionOracle,
    RoutingExpectation,
    SourceFixture,
    assert_members_preserved,
    expected_routing,
)
from tests.utils.lifecycle_interactions import ROUTING_ROWS, RoutingRow

if TYPE_CHECKING:
    from tests.integration.test_primitive_target_covering_array import _HookCoOwnerSetup

pytestmark = [pytest.mark.component, pytest.mark.lifecycle_smoke]


@pytest.fixture(autouse=True)
def canonical_native_roots() -> Iterator[None]:
    """Direct oracle controls use the same static-root boundary as native rows."""
    from tests.integration.test_primitive_target_covering_array import _canonical_native_roots

    with _canonical_native_roots():
        yield


def _invoke_root_control(tmp_path: Path, row: RoutingRow, entrypoint: str) -> None:
    """Exercise either supported caller without changing the injected-runner API."""
    from tests.integration import test_primitive_target_covering_array as execution

    binary = tmp_path / "unused-apm"
    if entrypoint == "public":
        execution.execute_row(tmp_path, binary, row)
    else:
        assert entrypoint == "direct"
        runner = execution.ApmLifecycleRunner((str(binary),))
        with runner.scenario(scenario_id=row.id):
            execution._execute_row(tmp_path, row, runner, time.monotonic(), None, [], None)


def test_injected_row_entry_owns_native_root_context() -> None:
    """Direct and public callers reach the same context before the execution body."""
    from tests.integration import test_primitive_target_covering_array as execution

    module = ast.parse(Path(execution.__file__).read_text(encoding="utf-8"))
    entry = next(
        (node for node in module.body if getattr(node, "name", "") == "_execute_row"), None
    )
    assert isinstance(entry, ast.FunctionDef), "Missing injected-runner entrypoint"
    contexts = [node for node in entry.body if isinstance(node, ast.With)]
    assert len(contexts) == 1, "Injected-runner entrypoint omitted native-root context"
    context = contexts[0]
    assert len(context.items) == 1
    expected = ast.parse("_canonical_native_roots(row)", mode="eval").body
    assert ast.dump(context.items[0].context_expr) == ast.dump(expected)
    assert len(context.body) == 1 and isinstance(context.body[0], ast.Return)
    call = context.body[0].value
    assert isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
    assert call.func.id == "_execute_row_body", "Isolation must enclose the real row body"
    assert [ast.dump(arg) for arg in call.args] == [
        ast.dump(ast.Name(id=arg.arg, ctx=ast.Load())) for arg in entry.args.args
    ]
    assert not call.keywords


@pytest.mark.parametrize("entrypoint", ("public", "direct"))
@pytest.mark.parametrize("row_id", ("claude-instructions-user", "hermes-skills-user"))
def test_configured_native_roots_are_isolated_before_fixture_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row_id: str, entrypoint: str
) -> None:
    """Stop at the first CLI boundary, after real source authoring and neighbor seeding."""
    from tests.integration import test_primitive_target_covering_array as execution

    external = tmp_path / "external"
    overrides = {
        "CLAUDE_CONFIG_DIR": external / "claude",
        "HERMES_HOME": external / "hermes",
    }
    for name, root in overrides.items():
        root.mkdir(parents=True)
        (root / "unrelated.txt").write_bytes(f"untouched {name}\n".encode())
        monkeypatch.setenv(name, str(root))
    before = ArtifactSnapshotSet.capture({"external": external})
    row = next(row for row in ROUTING_ROWS if row.id == row_id)
    seen = []
    seed_unowned = execution._seed_unowned

    def require_canonical_seed(oracle: InteractionOracle, lifetime: RoutingExpectation) -> None:
        # Check before writing: a future omission proof must never follow an
        # absolute override into _seed_unowned's home-relative neighbor layout.
        assert not set(overrides) & {name.upper() for name in os.environ}
        source = oracle.sources[0]
        expected = (
            f".claude/rules/{source.name}.md"
            if row.targets == ("claude",)
            else f".hermes/skills/{source.name}/SKILL.md"
        )
        assert lifetime.files == {expected}, "Configured root changed the authored relative route"
        seed_unowned(oracle, lifetime)

    def stop_before_cli(
        _runner: object,
        arguments: tuple[str, ...],
        *,
        scenario_id: str,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        assert arguments[0] == "install" and scenario_id == f"{row.id}-install"
        assert not set(overrides) & {name.upper() for name in os.environ}
        assert not set(overrides) & {name.upper() for name in env}
        profile = KNOWN_TARGETS[row.targets[0]].for_scope(user_scope=True)
        assert profile is not None
        assert profile.root_dir == f".{row.targets[0]}"
        assert (Path(env["HOME"]) / profile.root_dir / "unrelated.txt").read_bytes() == (
            b"unowned neighbor\n"
        )
        assert cwd.is_dir()
        seen.append(scenario_id)
        raise RuntimeError("configured-root control stopped before CLI")

    monkeypatch.setattr(execution, "_seed_unowned", require_canonical_seed)
    monkeypatch.setattr(execution.ApmLifecycleRunner, "run", stop_before_cli)
    with pytest.raises(RuntimeError, match="configured-root control stopped before CLI"):
        _invoke_root_control(tmp_path, row, entrypoint)
    assert seen == [f"{row.id}-install"]
    assert {name: os.environ.get(name) for name in overrides} == {
        name: str(root) for name, root in overrides.items()
    }
    assert_snapshot_set_unchanged(before, ArtifactSnapshotSet.capture({"external": external}))


@pytest.mark.parametrize("entrypoint", ("public", "direct"))
@pytest.mark.parametrize(
    "row", tuple(row for row in ROUTING_ROWS if row.dynamic_refusal), ids=lambda row: row.id
)
def test_unavailable_rows_strip_explicit_dynamic_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: RoutingRow, entrypoint: str
) -> None:
    """Only the existing refusal rows suppress explicit DB, skills and OneDrive roots."""
    from tests.integration import test_primitive_target_covering_array as execution

    external = tmp_path / "external"
    external.mkdir()
    db = external / "data.db"
    db.write_bytes(b"disposable app database sentinel\n")
    skills = external / "skills"
    skills.mkdir()
    (skills / "SKILL.md").write_bytes(b"disposable cowork skill sentinel\n")
    overrides = {
        "APM_COPILOT_APP_DB": str(db),
        "APM_COPILOT_COWORK_SKILLS_DIR": str(skills),
        "ONEDRIVE": str(external),
        "ONEDRIVECOMMERCIAL": str(external),
    }
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    before = ArtifactSnapshotSet.capture({"external": external})
    seen = []

    def stop_before_cli(
        _runner: object,
        arguments: tuple[str, ...],
        *,
        scenario_id: str,
        cwd: Path,
        env: Mapping[str, str],
    ) -> CommandResult:
        assert arguments[:2] == ("experimental", "enable")
        assert scenario_id.startswith(f"{row.id}-enable-")
        assert not overrides.keys() & os.environ.keys()
        assert not overrides.keys() & env.keys()
        assert not (Path(env["HOME"]) / ".copilot/data.db").exists()
        assert not (Path(env["HOME"]) / "Library/CloudStorage").exists()
        assert cwd.is_dir()
        seen.append(scenario_id)
        raise RuntimeError("dynamic-root control stopped before CLI")

    monkeypatch.setattr(execution.ApmLifecycleRunner, "run", stop_before_cli)
    with pytest.raises(RuntimeError, match="dynamic-root control stopped before CLI"):
        _invoke_root_control(tmp_path, row, entrypoint)
    assert len(seen) == 1
    assert {name: os.environ.get(name) for name in overrides} == overrides
    with execution._canonical_native_roots():
        assert {name: os.environ.get(name) for name in overrides} == overrides
    assert_snapshot_set_unchanged(before, ArtifactSnapshotSet.capture({"external": external}))


def test_native_root_context_restores_only_its_keys_and_rejects_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restoration is exception-safe without clearing unrelated process state."""
    from tests.integration.test_primitive_target_covering_array import _canonical_native_roots

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "external"))
    monkeypatch.delenv("HERMES_HOME", raising=False)
    with _canonical_native_roots():
        assert "CLAUDE_CONFIG_DIR" not in os.environ and "HERMES_HOME" not in os.environ
        monkeypatch.setenv("APM_NATIVE_ROOT_CONTROL", "keep")
    assert os.environ.get("CLAUDE_CONFIG_DIR") == str(tmp_path / "external")
    assert "HERMES_HOME" not in os.environ
    assert os.environ.get("APM_NATIVE_ROOT_CONTROL") == "keep"

    def enter_from_thread() -> None:
        with _canonical_native_roots():
            return

    with ThreadPoolExecutor(max_workers=1) as executor:
        with pytest.raises(AssertionError, match="requires the main thread"):
            executor.submit(enter_from_thread).result()
    assert os.environ.get("CLAUDE_CONFIG_DIR") == str(tmp_path / "external")


def _oracle(tmp_path: Path) -> InteractionOracle:
    project, home = tmp_path / "project", tmp_path / "home"
    project.mkdir()
    home.mkdir()
    return InteractionOracle(
        {"project": project, "user": home},
        "project",
        project,
        (
            SourceFixture(
                "fixture",
                "prompts",
                "task",
                "expected-marker",
                (".apm/prompts/task.prompt.md",),
                "fixture-org/fixture",
            ),
        ),
        RoutingRow("contract", ("prompts",), ("copilot",), False),
    )


@pytest.mark.parametrize(
    ("root_id", "relative"),
    (
        ("project", ".github/workflows/unrelated.yml"),
        ("user", ".config/unrelated-app/settings.json"),
        ("user", ".apm/unrelated.txt"),
        ("user", ".local/unrelated.txt"),
    ),
)
def test_exact_ancestors_reject_neighbor_overwrite(
    tmp_path: Path,
    root_id: str,
    relative: str,
) -> None:
    """A writable descendant never authorizes the rest of its parent tree."""
    oracle = _oracle(tmp_path)
    path = oracle.roots[root_id] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"unowned")
    with pytest.raises(AssertionError, match="outside its declared write set"):
        oracle.observe(
            "corruption",
            lambda: path.write_bytes(b"corrupted"),
            exact={
                "project": {".github/prompts/task.prompt.md"},
                "user": {
                    ".config/opencode/commands/task.md",
                    ".apm/config.json",
                    ".local/state/apm/lock",
                },
            },
        )


def test_exact_ancestor_entries_allow_legitimate_creation(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    path = oracle.roots["project"] / ".github/prompts/task.prompt.md"

    def create() -> None:
        path.parent.mkdir(parents=True)
        path.write_text("expected-marker")

    oracle.observe("install", create, exact={"project": {".github/prompts/task.prompt.md"}})
    oracle.evaluated("outcome.status_matches_state")
    oracle.assert_finished({"install"})
    assert path.read_text() == "expected-marker"


def test_exact_ancestor_permission_cannot_create_a_file(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    with pytest.raises(AssertionError, match="Writable ancestor is not a directory"):
        oracle.observe(
            "corrupt-ancestor",
            lambda: (oracle.roots["project"] / ".github").write_bytes(b"not a directory"),
            exact={"project": {".github/prompts/task.prompt.md"}},
        )


def _materialize_expected(oracle: InteractionOracle) -> LockFile:
    """Write actual native leaves and a serializable lock, not a ledger mock."""
    expected = expected_routing(oracle.row, oracle.sources)
    for relative in expected.files:
        path = oracle.roots[oracle.deployment_root_id] / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        markers = [
            source.marker
            for source in oracle.sources
            if relative in expected_routing(oracle.row, (source,)).files
        ]
        content = path.read_text() if relative in expected.shared and path.exists() else ""
        path.write_text(content + "\n".join(markers) + "\n", encoding="ascii")
    records = {
        str(index): DeploymentRecord(
            DeploymentLocator(
                LocatorKind.PROJECT_RELATIVE,
                target,
                path,
                None,
                "user" if path.startswith(".copilot/") else "project",
            ),
            owners,
            owners[-1],
            None,
        )
        for index, ((target, path), owners) in enumerate(expected.ledger.items())
    }
    lock = LockFile(
        dependencies={
            source.dependency_key: LockedDependency(
                repo_url=source.dependency_key,
                name=source.package_name,
                resolved_commit=f"{index + 1:040x}",
            )
            for index, source in enumerate(oracle.sources)
        },
        deployment_ledger=DeploymentLedger(records=records),
        _deployments_present=True,
    )
    lock.write(oracle.lock_root / "apm.lock.yaml")
    return lock


def test_omitted_ledger_record_fails_even_when_file_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    oracle.assert_routing(("copilot",))
    monkeypatch.setattr(
        "tests.utils.lifecycle_interaction_oracle.LockFile.read",
        lambda _path: SimpleNamespace(deployment_ledger=SimpleNamespace(records={})),
    )
    with pytest.raises(AssertionError, match="Ledger differs from source-derived routing"):
        oracle.assert_routing(("copilot",))


def test_wrong_target_claim_cannot_grant_write_permission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    unexpected = SimpleNamespace(
        locator=SimpleNamespace(target="cursor", value=".cursor/commands/task.md")
    )
    monkeypatch.setattr(
        "tests.utils.lifecycle_interaction_oracle.LockFile.read",
        lambda _path: SimpleNamespace(
            deployment_ledger=SimpleNamespace(records={"wrong": unexpected})
        ),
    )
    with pytest.raises(AssertionError, match="Ledger differs from source-derived routing"):
        oracle.assert_routing(("copilot",))


def test_removed_target_leak_detected_before_prune(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    oracle.assert_routing(("copilot",))
    leaked = ".cursor/commands/task.md"
    oracle.introduced.add(leaked)
    path = oracle.roots["project"] / leaked
    path.parent.mkdir(parents=True)
    path.write_text("stale widened deployment")
    with pytest.raises(AssertionError, match="Removed target leaked deployment"):
        oracle.assert_routing(("copilot",))


def test_never_authorized_target_without_ledger_is_rejected(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    forbidden = oracle.roots["project"] / ".cursor/commands/task.md"
    forbidden.parent.mkdir(parents=True)
    forbidden.write_text("expected-marker")
    with pytest.raises(AssertionError, match="Removed target leaked deployment"):
        oracle.assert_routing(("copilot",))


def test_missing_required_file_cannot_be_excused_by_ledger(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    (oracle.roots["project"] / ".github/prompts/task.prompt.md").unlink()
    with pytest.raises(AssertionError, match="Missing required deployment"):
        oracle.assert_routing(("copilot",))


def test_final_cleanup_checks_lifetime_not_initial_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oracle = _oracle(tmp_path)
    leaked = ".cursor/commands/introduced-after-first-install.md"
    oracle.introduced.add(leaked)
    path = oracle.roots["project"] / leaked
    path.parent.mkdir(parents=True)
    path.write_text("late ownership")
    with pytest.raises(AssertionError, match="Removed target leaked deployment"):
        oracle.assert_routing(())


def test_shared_config_preservation_uses_catalog_not_filename_suffix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oracle = _oracle(tmp_path)
    oracle.row = replace(oracle.row, primitives=("hooks",), targets=("claude",))
    oracle.sources = (replace(oracle.sources[0], primitive="hooks"),)
    monkeypatch.setitem(
        KNOWN_TARGETS,
        "claude",
        replace(KNOWN_TARGETS["claude"], hooks_config_display=".claude/hook-config.yaml"),
    )
    path = oracle.roots["project"] / ".claude/hook-config.yaml"
    path.parent.mkdir()
    path.write_text("user: keep\n", encoding="ascii")
    oracle.assert_routing(())
    path.write_text("hook: expected-marker\n", encoding="ascii")
    with pytest.raises(AssertionError, match="Removed target leaked shared ownership"):
        oracle.assert_routing(())


def test_source_provenance_rejects_swapped_dependency_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_primitive_target_covering_array import _assert_provenance

    oracle = _oracle(tmp_path)
    oracle.sources = (
        *oracle.sources,
        replace(oracle.sources[0], package_name="child", dependency_key="fixture-org/child"),
    )
    monkeypatch.setattr(
        "tests.utils.lifecycle_interaction_oracle.LockFile.read",
        lambda _path: SimpleNamespace(
            get_package_dependencies=lambda: (
                LockedDependency(
                    repo_url="fixture-org/fixture", name="fixture", resolved_commit="child-commit"
                ),
                LockedDependency(
                    repo_url="fixture-org/child", name="child", resolved_commit="parent-commit"
                ),
            )
        ),
    )
    with pytest.raises(AssertionError):
        _assert_provenance(oracle, {"fixture": "parent-commit", "child": "child-commit"})


def test_known_gap_does_not_allow_configuration_rewrites(tmp_path: Path) -> None:
    from tests.integration.test_primitive_target_covering_array import _assert_reinstall

    oracle = _oracle(tmp_path)
    oracle.row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    path = oracle.roots["user"] / ".apm/config.json"
    path.parent.mkdir()
    path.write_text('{"keep": true}\n', encoding="ascii")
    before = oracle.capture()
    path.write_text('{"keep": false}\n', encoding="ascii")
    with pytest.raises(AssertionError, match="outside its declared write set"):
        _assert_reinstall(oracle, before, None)


def test_skipped_transition_assertions_fail_closed(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    oracle.observe("narrow", lambda: None, unchanged=True)
    with pytest.raises(AssertionError, match="Skipped transition assertions: narrow"):
        oracle.observe("prune", lambda: None, unchanged=True)
    with pytest.raises(AssertionError, match="Skipped transition assertions: narrow"):
        oracle.assert_finished({"narrow", "prune"})


def test_deleted_transition_cannot_vacuously_finish(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    oracle.observe("install", lambda: None, unchanged=True)
    oracle.evaluated()
    with pytest.raises(AssertionError, match="Missing evaluated transitions"):
        oracle.assert_finished({"install", "widen", "narrow", "prune"})


@pytest.mark.parametrize(
    "corrupt",
    (
        {"foreign": {"keep": False}, "hooks": ["foreign-hook"]},
        {"foreign": {"keep": True}, "hooks": []},
        {"hooks": ["foreign-hook"]},
    ),
)
def test_shared_config_corruption_rejected_despite_exact_file_permission(
    tmp_path: Path,
    corrupt: dict,
) -> None:
    oracle = _oracle(tmp_path)
    path = oracle.roots["project"] / ".claude/settings.json"
    path.parent.mkdir()
    before = {"foreign": {"keep": True}, "hooks": ["foreign-hook"]}
    path.write_text(json.dumps(before))
    oracle.protected_json[path] = before
    with pytest.raises(AssertionError, match="Shared"):
        oracle.observe(
            "overwrite-shared",
            lambda: path.write_text(json.dumps(corrupt)),
            exact={"project": {".claude/settings.json"}},
        )


def test_shared_member_preservation_allows_another_owner() -> None:
    expected = {"hooks": ["surviving-package"], "user": {"keep": True}}
    actual = {"hooks": ["surviving-package", "new-package"], "user": {"keep": True}, "new": 1}
    assert_members_preserved(expected, actual)
    assert actual["hooks"] == ["surviving-package", "new-package"]


def test_failed_case_reports_partial_evidence_without_credit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed actions remain failures and never inherit the required-law inventory."""
    from tests.integration import test_primitive_target_covering_array as execution

    oracle = _oracle(tmp_path)
    emitted = []

    def fail_after_install(*arguments: object) -> None:
        observed = arguments[-2]
        assert isinstance(observed, list)
        observed.append(oracle)
        oracle.observe("install", lambda: None, unchanged=True)
        oracle.evaluated("outcome.status_matches_state")
        oracle.observe("audit", lambda: None, unchanged=True)
        raise AssertionError("deliberate failed action")

    monkeypatch.setattr(execution, "_execute_row", fail_after_install)
    with pytest.raises(AssertionError, match="deliberate failed action"):
        execution.execute_row(
            tmp_path,
            tmp_path / "unused-apm",
            oracle.row,
            record_execution=emitted.append,
        )
    assert len(emitted) == 1
    assert emitted[0].status == "failed"
    assert emitted[0].transitions == ("install", "audit")
    assert "idempotency.byte_stable" not in emitted[0].evaluated_laws
    assert "routing.authorized_targets_only" not in emitted[0].evaluated_laws
    assert json.loads((tmp_path / "lifecycle-execution.json").read_text())["status"] == "failed"


def test_source_provenance_rejects_swapped_deployment_owners(tmp_path: Path) -> None:
    """Two valid dependencies and unchanged native bytes cannot excuse swapped claims."""
    from tests.integration.test_primitive_target_covering_array import _assert_provenance

    oracle = _oracle(tmp_path)
    oracle.sources = (
        oracle.sources[0],
        replace(
            oracle.sources[0],
            package_name="child",
            name="child-task",
            marker="child-marker",
            dependency_key="fixture-org/child",
            source_files=(".apm/prompts/child-task.prompt.md",),
        ),
    )
    lock = _materialize_expected(oracle)
    oracle.assert_routing(("copilot",))
    commits = {dep.name: dep.resolved_commit for dep in lock.dependencies.values()}
    _assert_provenance(oracle, commits)
    before = {
        name: (oracle.roots["project"] / name).read_bytes()
        for name in expected_routing(oracle.row, oracle.sources).files
    }
    owners = [source.dependency_key for source in oracle.sources]
    swapped = dict(zip(owners, reversed(owners), strict=True))
    lock.deployment_ledger = DeploymentLedger(
        records={
            key: replace(
                record,
                owners=(swapped[record.active_owner],),
                active_owner=swapped[record.active_owner],
            )
            for key, record in lock.deployment_ledger.records.items()
        }
    )
    lock.write(oracle.lock_root / "apm.lock.yaml")
    _assert_provenance(oracle, commits)
    assert all(
        (oracle.roots["project"] / path).read_bytes() == data for path, data in before.items()
    )
    with pytest.raises(AssertionError, match="Ledger ownership differs from authored source"):
        oracle.assert_routing(("copilot",))


def test_source_provenance_checks_active_owner_with_valid_owner_set(tmp_path: Path) -> None:
    """A shared source-derived claim does not let any of its owners become active."""
    oracle = _oracle(tmp_path)
    oracle.row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    oracle.deployment_root_id = "user"
    oracle.lock_root = oracle.roots["user"] / ".apm"
    oracle.lock_root.mkdir()
    oracle.sources = (
        replace(oracle.sources[0], primitive="instructions"),
        replace(
            oracle.sources[0],
            primitive="instructions",
            package_name="child",
            marker="child-marker",
            dependency_key="fixture-org/child",
        ),
    )
    lock = _materialize_expected(oracle)
    oracle.assert_routing(("copilot",))
    assert len(lock.deployment_ledger.records) == 1
    lock.deployment_ledger = DeploymentLedger(
        records={
            key: replace(record, active_owner=record.owners[0])
            for key, record in lock.deployment_ledger.records.items()
        }
    )
    lock.write(oracle.lock_root / "apm.lock.yaml")
    with pytest.raises(AssertionError, match="Ledger ownership differs from authored source"):
        oracle.assert_routing(("copilot",))


@pytest.mark.parametrize(
    ("field", "value"),
    (("kind", LocatorKind.TARGET_RELATIVE), ("scope", "user"), ("runtime", "copilot")),
)
def test_native_locator_metadata_cannot_be_relabelled(
    tmp_path: Path, field: str, value: str
) -> None:
    oracle = _oracle(tmp_path)
    lock = _materialize_expected(oracle)
    oracle.assert_routing(("copilot",))
    lock.deployment_ledger = DeploymentLedger(
        records={
            key: replace(record, locator=replace(record.locator, **{field: value}))
            for key, record in lock.deployment_ledger.records.items()
        }
    )
    lock.write(oracle.lock_root / "apm.lock.yaml")
    with pytest.raises(AssertionError, match="Ledger locator metadata differs"):
        oracle.assert_routing(("copilot",))


def test_exact_native_file_requires_regular_leaf(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    oracle.assert_routing(("copilot",))
    path = oracle.roots["project"] / ".github/prompts/task.prompt.md"
    referent = oracle.roots["project"] / "foreign-referent.txt"
    referent.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(referent)
    with pytest.raises(AssertionError, match="Native fixture leaf is not a regular file"):
        oracle.assert_routing(("copilot",))
    with pytest.raises(AssertionError, match="Native fixture leaf is not a regular file"):
        oracle.observe("audit", lambda: None, unchanged=True)


def test_exact_native_file_rejects_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unchanged links hide external byte changes, so validate before live reads."""
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    oracle.assert_routing(("copilot",))
    native = oracle.roots["project"] / ".github/prompts/task.prompt.md"
    outside = tmp_path / "outside-observed-roots.txt"
    outside.write_bytes(native.read_bytes())
    native.unlink()
    native.symlink_to(outside)
    before = oracle.capture()
    outside.write_bytes(b"expected-marker changed outside both roots\n")
    assert_snapshot_set_unchanged(before, oracle.capture())
    original_bytes, original_text = Path.read_bytes, Path.read_text

    def guarded_bytes(path: Path) -> bytes:
        assert path.resolve() != outside.resolve(), "Oracle read an external referent"
        return original_bytes(path)

    def guarded_text(path: Path, *args: object, **kwargs: object) -> str:
        assert path.resolve() != outside.resolve(), "Oracle read an external referent"
        return original_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_bytes)
    monkeypatch.setattr(Path, "read_text", guarded_text)
    with pytest.raises(PathTraversalError, match="outside"):
        oracle.assert_routing(("copilot",))
    with pytest.raises(PathTraversalError, match="outside"):
        oracle.observe("audit", lambda: None, unchanged=True)
    assert oracle.evaluations == []


def test_native_leaf_guard_does_not_outlaw_fixture_cache_links(tmp_path: Path) -> None:
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    source = tmp_path / "authored-source"
    source.mkdir()
    (source / "apm.yml").write_text("name: cache-fixture\n", encoding="ascii")
    link = oracle.roots["project"] / "apm_modules/cache-alias"

    def materialize() -> None:
        link.parent.mkdir()
        link.symlink_to(source, target_is_directory=True)

    oracle.observe("cache-link", materialize, exact={"project": {"apm_modules/cache-alias"}})
    oracle.assert_routing(("copilot",))
    oracle.evaluated()
    assert link.is_symlink() and (link / "apm.yml").read_bytes() == b"name: cache-fixture\n"


def test_native_containment_rejects_post_snapshot_symlink_escape(tmp_path: Path) -> None:
    """Containment is independently checked against the live path, not cached kind."""
    oracle = _oracle(tmp_path)
    _materialize_expected(oracle)
    entries = {entry.relative_path: entry for entry in oracle.capture().snapshot("project").entries}
    relative = ".github/prompts/task.prompt.md"
    native = oracle.roots["project"] / relative
    outside = tmp_path / "outside.txt"
    outside.write_bytes(native.read_bytes())
    native.unlink()
    native.symlink_to(outside)
    assert entries[relative].kind == "file"
    with pytest.raises(PathTraversalError, match="outside"):
        oracle._native_path(relative, entries)


def _refusal_receipt(root: Path, target: str, *, linux: bool = False) -> CommandResult:
    """Author independently recognizable root diagnostics, not production formatting."""
    diagnostic = (
        "GitHub Copilot desktop App not detected.\n"
        "Expected ~/.copilot/data.db but the file is missing."
        if target == "copilot-app"
        else (
            "Cowork has no auto-detection on Linux."
            if linux
            else "Cowork: no OneDrive path detected."
        )
    )
    recovery = (
        "Install the app, or omit '--target copilot-app'."
        if target == "copilot-app"
        else (
            "Set APM_COPILOT_COWORK_SKILLS_DIR or run: "
            "apm config set copilot-cowork-skills-dir <path>"
        )
    )
    return CommandResult(
        ("apm", "install", "--global", "--target", target),
        1,
        diagnostic,
        recovery,
        root,
    )


@pytest.mark.parametrize(
    ("target", "linux"),
    (("copilot-app", False), ("copilot-cowork", False), ("copilot-cowork", True)),
)
def test_refusal_receipt_credits_only_unavailable_root(
    tmp_path: Path, target: str, linux: bool
) -> None:
    """Both native diagnostics survive stream splitting, styling and line wrapping."""
    from tests.integration.test_primitive_target_covering_array import _result

    oracle = _oracle(tmp_path)
    result = _refusal_receipt(oracle.roots["project"], target, linux=linux)
    result = replace(
        result,
        stdout=f"\x1b[31m[x] {result.stdout.replace(' ', '  ')}\x1b[0m",
        stderr=result.stderr.replace(" or ", "\n or "),
    )
    observed = oracle.observe("refusal", lambda: result, unchanged=True)
    _result(observed, "refusal", success=False, refusal_target=target)
    oracle.evaluated("outcome.status_matches_state", "transaction.failed_command_preserves_state")
    oracle.assert_finished({"refusal"})
    assert oracle.evaluations[-1][1][-2:] == (
        "outcome.status_matches_state",
        "transaction.failed_command_preserves_state",
    )


@pytest.mark.parametrize("target", ("copilot-app", "copilot-cowork"))
@pytest.mark.parametrize(
    "fault",
    (
        "usage",
        "signal",
        "traceback",
        "wrong-status",
        "success-status",
        "boolean-status",
        "float-status",
        "wrong-target",
        "missing-target",
        "missing-diagnostic",
        "missing-recovery",
    ),
)
def test_refusal_receipt_rejects_false_credit(tmp_path: Path, target: str, fault: str) -> None:
    """An unchanged filesystem cannot turn an unrelated failure into refusal credit."""
    from tests.integration.test_primitive_target_covering_array import _result

    oracle = _oracle(tmp_path)
    root = oracle.roots["project"]
    result = _refusal_receipt(root, target)
    if fault in {"usage", "signal", "traceback"}:
        code, output = {
            "usage": (2, "Usage: apm install [OPTIONS]\nError: No such option: --bad"),
            "signal": (-9, ""),
            "traceback": (1, "Traceback (most recent call last):\nRuntimeError: unrelated crash"),
        }[fault]
        result = replace(result, returncode=code, stdout=output, stderr="")
    elif fault in {"wrong-status", "success-status"}:
        result = replace(result, returncode=2 if fault == "wrong-status" else 0)
    elif fault in {"boolean-status", "float-status"}:
        result = replace(result, returncode=True if fault == "boolean-status" else 1.0)
    elif fault == "wrong-target":
        other = "copilot-cowork" if target == "copilot-app" else "copilot-app"
        result = _refusal_receipt(root, other)
    elif fault == "missing-diagnostic":
        result = replace(result, stdout="")
    elif fault == "missing-recovery":
        result = replace(result, stderr="")
    before = oracle.capture()
    observed = oracle.observe("refusal", lambda: result, unchanged=True)
    with pytest.raises(AssertionError, match="refusal:"):
        _result(
            observed,
            "refusal",
            success=False,
            refusal_target=None if fault == "missing-target" else target,
        )
        oracle.evaluated(
            "outcome.status_matches_state", "transaction.failed_command_preserves_state"
        )
    assert oracle.evaluations == [] and oracle.pending == "refusal"
    assert_snapshot_set_unchanged(before, oracle.capture())


def test_refusal_receipt_cannot_excuse_any_root_write(tmp_path: Path) -> None:
    """Even a correctly attributed refusal cannot modify an unowned neighboring file."""
    oracle = _oracle(tmp_path)
    neighbor = oracle.roots["user"] / "unrelated.txt"
    neighbor.write_bytes(b"unowned\n")
    receipt = _refusal_receipt(oracle.roots["project"], "copilot-app")

    def corrupt() -> CommandResult:
        neighbor.write_bytes(b"corrupted\n")
        return receipt

    with pytest.raises(AssertionError, match="changed"):
        oracle.observe("refusal", corrupt, unchanged=True)
    assert oracle.evaluations == []


def test_refusal_validation_uses_authored_target_before_credit() -> None:
    """The real action must pass its row target and retain full-root no-write checks."""
    from tests.integration import test_primitive_target_covering_array as execution

    module = ast.parse(Path(execution.__file__).read_text(encoding="utf-8"))
    outer = next(
        (node for node in module.body if getattr(node, "name", "") == "_execute_row_body"), None
    )
    assert isinstance(outer, ast.FunctionDef), "Missing isolated row body"
    action = next(node for node in outer.body if getattr(node, "name", "") == "action")
    calls = [node for node in ast.walk(action) if isinstance(node, ast.Call)]
    validation = next(
        (node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "_result"),
        None,
    )
    assert validation is not None, "CLI action omitted receipt validation"
    keywords = {keyword.arg: keyword.value for keyword in validation.keywords}
    assert "refusal_target" in keywords, "Refusal validation omitted the authored target"
    expected = ast.parse("row.targets[0] if row.dynamic_refusal else None", mode="eval").body
    assert ast.dump(keywords["refusal_target"]) == ast.dump(expected)
    credit = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "evaluated"
    )
    observation = next(
        node
        for node in calls
        if isinstance(node.func, ast.Attribute) and node.func.attr == "observe"
    )
    assert observation.lineno < validation.lineno < credit.lineno
    observation_keywords = {keyword.arg: keyword.value for keyword in observation.keywords}
    assert "unchanged" in observation_keywords, "Action omitted the no-write observation"
    assert ast.dump(observation_keywords["unchanged"]) == ast.dump(
        ast.Name(id="unchanged", ctx=ast.Load())
    ), "Action did not forward the no-write requirement"
    refusal = next(
        node
        for node in ast.walk(outer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "action"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "refusal"
    )
    refusal_keywords = {keyword.arg: keyword.value for keyword in refusal.keywords}
    for name, value in (("success", False), ("unchanged", True)):
        assert name in refusal_keywords, f"Refusal omitted {name}"
        assert isinstance(refusal_keywords[name], ast.Constant)
        assert refusal_keywords[name].value is value


def _audit_receipt(root: Path, *, clean: bool, hash_failure: bool = True) -> CommandResult:
    """Author public --ci JSON independently of the production result serializer."""
    path = ".github/prompts/task.prompt.md"
    names = (
        "lockfile-exists",
        "ref-consistency",
        "deployment-ledger-owners",
        "deployed-files-present",
        "no-orphaned-packages",
        "skill-subset-consistency",
        "config-consistency",
        "content-integrity",
        "includes-consent",
        "drift",
    )
    checks = [
        {"name": name, "passed": True, "message": "completed", "details": []} for name in names
    ]
    checks[-1]["message"] = "no drift detected against lockfile"
    if not clean:
        checks[-1].update(
            passed=False,
            details=[f"modified: {path}"],
            message=f"drift detected: 1 file(s): {path}",
        )
        if hash_failure:
            checks[-3].update(
                passed=False,
                details=[
                    f"hash-drift: {path} (dep=fixture-org/fixture, expected=abc..., actual=def...)"
                ],
            )
    failed = sum(not check["passed"] for check in checks)
    payload = {
        "passed": clean,
        "checks": checks,
        "summary": {"total": len(checks), "passed": len(checks) - failed, "failed": failed},
        "drift": {
            "drift": [] if clean else [{"path": path, "kind": "modified", "package": "fixture"}]
        },
    }
    return CommandResult(
        ("apm", "audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json"),
        0 if clean else 1,
        json.dumps(payload),
        "",
        root,
    )


@pytest.mark.parametrize(("clean", "hash_failure"), ((True, False), (False, True), (False, False)))
def test_audit_receipt_credits_only_completed_fixture_verification(
    tmp_path: Path, clean: bool, hash_failure: bool
) -> None:
    from tests.integration.test_primitive_target_covering_array import _result

    oracle = _oracle(tmp_path)
    root = oracle.roots["project"]
    result = _audit_receipt(root, clean=clean, hash_failure=hash_failure)
    observed = oracle.observe("audit", lambda: result, unchanged=True)
    _result(
        observed,
        "audit",
        success=clean,
        deployment_root=root,
        tampered_path=None if clean else root / ".github/prompts/task.prompt.md",
    )
    oracle.evaluated("outcome.status_matches_state")
    oracle.assert_finished({"audit"})
    assert oracle.evaluations[-1][1][-1] == "outcome.status_matches_state"


@pytest.mark.parametrize("clean", (True, False), ids=("clean", "tampered"))
@pytest.mark.parametrize(
    "fault",
    (
        "usage",
        "signal",
        "traceback",
        "malformed-json",
        "not-object",
        "status-only",
        "nonboolean-passed",
        "wrong-status",
        "wrong-passed",
        "missing-check",
        "duplicate-check",
        "nonboolean-check",
        "unrelated-check",
        "bad-summary",
        "missing-replay",
        "replay-error",
        "replay-read-error",
        "skipped-replay",
        "unrelated-path",
        "wrong-kind",
        "unrelated-integrity",
    ),
)
def test_audit_receipt_rejects_false_credit(tmp_path: Path, clean: bool, fault: str) -> None:
    from tests.integration.test_primitive_target_covering_array import _result

    oracle = _oracle(tmp_path)
    root = oracle.roots["project"]
    result = _audit_receipt(root, clean=clean)
    payload = json.loads(result.stdout)
    checks = {check["name"]: check for check in payload["checks"]}
    if fault in {"usage", "signal", "traceback", "malformed-json", "not-object"}:
        code, stdout = {
            "usage": (2, "Usage: apm audit [OPTIONS]"),
            "signal": (-9, ""),
            "traceback": (1, "Traceback: unexpected crash"),
            "malformed-json": (result.returncode, "{"),
            "not-object": (result.returncode, "[]"),
        }[fault]
        result = replace(result, returncode=code, stdout=stdout)
    elif fault == "wrong-status":
        result = replace(result, returncode=1 if clean else 0)
    elif fault == "status-only":
        result = replace(result, stdout=json.dumps({"passed": clean}))
    else:
        if fault == "nonboolean-passed":
            payload["passed"] = int(clean)
        elif fault == "wrong-passed":
            payload["passed"] = not clean
        elif fault == "missing-check":
            payload["checks"].remove(checks["config-consistency"])
        elif fault == "duplicate-check":
            payload["checks"].append(checks["content-integrity"])
        elif fault == "nonboolean-check":
            checks["content-integrity"]["passed"] = int(clean)
        elif fault == "unrelated-check":
            checks["ref-consistency"]["passed"] = False
        elif fault == "bad-summary":
            payload["summary"]["total"] = 0
        elif fault == "missing-replay":
            del payload["drift"]
        elif fault == "replay-error":
            checks["drift"].update(passed=False, details=["replay failed"])
            payload["drift"]["drift"] = []
        elif fault == "replay-read-error":
            payload["drift"]["drift"] = [
                {
                    "path": ".github/prompts/task.prompt.md",
                    "kind": "modified",
                    "package": "fixture",
                    "inline_diff": "(read error: invalid hook projection)",
                }
            ]
        elif fault == "skipped-replay":
            checks["drift"].update(passed=True, message="drift skipped: cache not populated")
            payload["drift"]["drift"] = []
        elif fault in {"unrelated-path", "wrong-kind"}:
            payload["drift"]["drift"] = [
                {
                    "path": ".github/prompts/neighbor.prompt.md"
                    if fault == "unrelated-path"
                    else ".github/prompts/task.prompt.md",
                    "kind": "modified" if fault == "unrelated-path" else "missing",
                }
            ]
        elif fault == "unrelated-integrity":
            checks["content-integrity"].update(
                passed=False, details=["unresolved: .github/prompts/task.prompt.md"]
            )
        if fault != "bad-summary":
            failed = sum(not check["passed"] for check in payload["checks"])
            payload["summary"] = {
                "total": len(payload["checks"]),
                "passed": len(payload["checks"]) - failed,
                "failed": failed,
            }
        result = replace(result, stdout=json.dumps(payload))
    oracle.observe("audit", lambda: result, unchanged=True)
    with pytest.raises(AssertionError, match="audit:"):
        _result(
            result,
            "audit",
            success=clean,
            deployment_root=root,
            tampered_path=None if clean else root / ".github/prompts/task.prompt.md",
        )
        oracle.evaluated("outcome.status_matches_state")
    assert oracle.evaluations == [] and oracle.pending == "audit"


def test_audit_validation_precedes_law_credit_at_shared_action_boundary() -> None:
    """Static boundary trap: real rows cannot bypass the tested receipt owner."""
    from tests.integration import test_primitive_target_covering_array as execution
    from tests.utils import lifecycle_model_driver

    module = ast.parse(Path(execution.__file__).read_text(encoding="utf-8"))
    outer = next(
        (
            node
            for node in module.body
            if isinstance(node, ast.FunctionDef) and node.name == "_execute_row_body"
        ),
        None,
    )
    assert isinstance(outer, ast.FunctionDef), "Missing _execute_row_body action boundary"
    action = next(
        (
            node
            for node in outer.body
            if isinstance(node, ast.FunctionDef) and node.name == "action"
        ),
        None,
    )
    assert isinstance(action, ast.FunctionDef), "Missing real CLI action function"
    calls = [node for node in ast.walk(action) if isinstance(node, ast.Call)]
    validation = next(
        (node for node in calls if isinstance(node.func, ast.Name) and node.func.id == "_result"),
        None,
    )
    assert validation is not None, "CLI action omitted _result validation before law credit"
    credit = next(
        (
            node
            for node in calls
            if isinstance(node.func, ast.Attribute) and node.func.attr == "evaluated"
        ),
        None,
    )
    assert credit is not None, "CLI action omitted evaluated law boundary"
    assert validation.lineno < credit.lineno
    assert {"deployment_root", "tampered_path"} <= {kw.arg for kw in validation.keywords}
    assert "--no-fail-fast" in {
        node.value
        for node in ast.walk(outer)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert execution.assert_ci_audit_result is lifecycle_model_driver.assert_ci_audit_result
    driver = ast.parse(Path(lifecycle_model_driver.__file__).read_text(encoding="utf-8"))
    outcome = next(
        (
            node
            for node in driver.body
            if isinstance(node, ast.FunctionDef) and node.name == "law_outcome"
        ),
        None,
    )
    assert isinstance(outcome, ast.FunctionDef), "Missing model law_outcome boundary"
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "assert_ci_audit_result"
        for node in ast.walk(outcome)
    )


def _shared_hook_oracle(tmp_path: Path, row: RoutingRow | None = None) -> InteractionOracle:
    """Seed the existing user-scope widening row, including its initially inactive file."""
    from tests.integration.test_primitive_target_covering_array import (
        _author_hook_coowner,
        _hook_coowner,
        _seed_unowned,
        _source_dependency,
    )
    from tests.utils.local_package import LocalPackageFactory

    oracle = _oracle(tmp_path)
    oracle.row = row or next(
        row for row in ROUTING_ROWS if row.id == "claude-codex-hook-widen-narrow-user"
    )
    oracle.deployment_root_id = "user" if oracle.row.user_scope else "project"
    oracle.sources = (replace(oracle.sources[0], primitive="hooks"),)
    oracle.lock_root = (
        oracle.roots["user"] / ".apm" if oracle.row.user_scope else oracle.roots["project"]
    )
    targets = oracle.row.widen_targets or oracle.row.targets
    factory = LocalPackageFactory(tmp_path / "sources")
    package = factory.create("persistent-hooks", targets=targets)
    source = _author_hook_coowner(factory, package, targets)
    dependency = _source_dependency(
        package,
        remote="https://gitlab.example.invalid/coowner-org/persistent-hooks.git",
        parent=None,
    )
    oracle.hook_coowner = _hook_coowner(
        package, source, dependency, oracle.row, targets, oracle.lock_root
    )
    for path, content in oracle.hook_coowner.materialized_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    lifetime = expected_routing(oracle.row, oracle.sources, targets)
    _seed_unowned(oracle, lifetime)
    LockFile(
        dependencies={
            dependency.get_unique_key(): LockedDependency.from_dependency_ref(
                dependency, "a" * 40, 1, None, package_name=package.name
            )
        }
    ).write(oracle.lock_root / "apm.lock.yaml")
    return oracle


def _write_shared_hook_fixture(oracle: InteractionOracle, targets: tuple[str, ...]) -> None:
    """Simulate adding/removing only this fixture's hook; never replace the foreign slice."""
    root = oracle.roots[oracle.deployment_root_id]
    source = oracle.sources[0]
    assert oracle.hook_coowner is not None
    authorized = targets or oracle.hook_targets
    for target, members in oracle.hook_coowner.members.items():
        for name, protected in members.items():
            path = root / name
            sidecar = path.name == "apm-hooks.json"
            payload = json.loads(json.dumps(oracle.protected_json.get(path, {})))
            if target in authorized:
                for container, entries in protected.items():
                    if isinstance(entries, list):
                        payload[container] = json.loads(json.dumps(entries))
                    elif isinstance(entries, dict):
                        for event, hooks in entries.items():
                            payload.setdefault(container, {}).setdefault(event, [])[:0] = (
                                json.loads(json.dumps(hooks))
                            )
                    else:
                        payload[container] = entries
            if target in targets:
                events = payload if sidecar else payload["hooks"]
                event = next(iter(events))
                owned = json.loads(
                    json.dumps(events[event][0]).replace(
                        "lifecycle-unowned-foreign-package", source.marker
                    )
                )
                if sidecar:
                    owned["_apm_source"] = source.package_name
                events[event].append(owned)
            if payload:
                path.write_text(json.dumps(payload) + "\n", encoding="ascii")
            else:
                path.unlink(missing_ok=True)


def test_authored_shared_members_survive_existing_lifecycle_row(tmp_path: Path) -> None:
    oracle = _shared_hook_oracle(tmp_path)
    root = oracle.roots["user"]
    assert oracle.hook_coowner is not None
    exact = {"user": {name for members in oracle.hook_coowner.members.values() for name in members}}
    steps = (
        ("install", oracle.row.targets),
        ("reinstall", oracle.row.targets),
        ("widen", oracle.row.widen_targets),
        ("reinstall-widened", oracle.row.widen_targets),
        ("narrow", oracle.row.narrow_targets),
        ("uninstall", ()),
    )
    for operation, targets in steps:
        oracle.observe(
            operation,
            lambda targets=targets: _write_shared_hook_fixture(oracle, targets),
            exact=exact,
            hook_targets=targets if operation != "uninstall" else None,
        )
        oracle.assert_routing(targets)
        oracle.assert_hook_coowner_installed()
        oracle.evaluated("routing.authorized_targets_only")
    oracle.assert_finished(operation for operation, _targets in steps)
    assert len(oracle.protected_json) == 2
    for path, protected in oracle.protected_json.items():
        assert_members_preserved(protected, json.loads(path.read_text()))
    assert (root / ".claude/apm-hooks.json").exists()
    assert not (root / ".codex/apm-hooks.json").exists()


@pytest.mark.parametrize("member", ("native-user", "native-package", "sidecar"))
def test_foreign_shared_member_loss_fails_with_fixture_marker_and_ledger_intact(
    tmp_path: Path, member: str
) -> None:
    oracle = _shared_hook_oracle(tmp_path)
    targets = oracle.row.widen_targets
    _write_shared_hook_fixture(oracle, targets)
    oracle.hook_targets = targets
    oracle.assert_routing(targets)
    name = "apm-hooks.json" if member == "sidecar" else "hooks.json"
    path = oracle.roots["user"] / ".codex" / name
    payload = json.loads(path.read_text())
    events = payload if member == "sidecar" else payload["hooks"]
    entries = events[next(iter(events))]
    del entries[1 if member == "native-user" else 0]
    lock_before = (oracle.lock_root / "apm.lock.yaml").read_bytes()
    credit_before = list(oracle.evaluations)
    with pytest.raises(AssertionError, match="Shared"):
        oracle.observe(
            "narrow",
            lambda: path.write_text(json.dumps(payload), encoding="ascii"),
            exact={"user": {path.relative_to(oracle.roots["user"]).as_posix()}},
        )
    assert oracle.sources[0].marker in path.read_text()
    assert (oracle.lock_root / "apm.lock.yaml").read_bytes() == lock_before
    assert oracle.evaluations == credit_before


@pytest.mark.parametrize(
    ("target", "container", "event"),
    (
        ("cursor", "hooks", "beforeShellExecution"),
        ("gemini", "hooks", "BeforeTool"),
        ("antigravity", "apm", "PreToolUse"),
        ("windsurf", "hooks", "pre_run_command"),
    ),
)
def test_shared_hook_seeds_are_native_foreign_members_in_existing_rows(
    tmp_path: Path, target: str, container: str, event: str
) -> None:
    row = next(
        row
        for row in ROUTING_ROWS
        if row.catalog_cell
        and row.targets == (target,)
        and row.primitives == ("hooks",)
        and not row.user_scope
    )
    oracle = _shared_hook_oracle(tmp_path, row)
    assert len(oracle.protected_json) == 1
    native_path = next(path for path in oracle.protected_json if path.name != "apm-hooks.json")
    sidecar_path = native_path.parent / "apm-hooks.json"
    native = json.loads(native_path.read_text())
    assert not sidecar_path.exists(), "Uninstalled co-owner cannot seed an orphan sidecar"
    assert len(native[container][event]) == 1
    assert "user-entry" in json.dumps(native)
    assert "_apm_source" not in json.dumps(native)
    assert oracle.hook_coowner is not None
    members = oracle.hook_coowner.members[target]
    expected_sidecar = members[sidecar_path.relative_to(oracle.roots["project"]).as_posix()]
    assert expected_sidecar[event][0]["_apm_source"] == "coowner-org/persistent-hooks"
    oracle.assert_routing(())


@pytest.mark.parametrize("fault", ("lock-owner", "source-bytes", "source-missing"))
def test_persistent_hook_coowner_requires_installed_authored_inputs(
    tmp_path: Path, fault: str
) -> None:
    oracle = _shared_hook_oracle(tmp_path)
    oracle.assert_hook_coowner_installed()
    assert oracle.hook_coowner is not None
    if fault == "lock-owner":
        LockFile().write(oracle.lock_root / "apm.lock.yaml")
    else:
        path = next(iter(oracle.hook_coowner.materialized_files))
        if fault == "source-missing":
            path.unlink()
        else:
            path.write_bytes(b"changed co-owner source")
    with pytest.raises(AssertionError, match="Persistent hook co-owner"):
        oracle.assert_hook_coowner_installed()


def test_narrowing_rejects_coowner_on_deauthorized_target(tmp_path: Path) -> None:
    oracle = _shared_hook_oracle(tmp_path)
    _write_shared_hook_fixture(oracle, oracle.row.widen_targets)
    oracle.hook_targets = oracle.row.widen_targets
    oracle.assert_protected_content(oracle.capture())
    with pytest.raises(AssertionError, match="Deauthorized co-owner hook survived"):
        oracle.assert_protected_content(oracle.capture(), hook_targets=oracle.row.narrow_targets)


def test_user_hook_cannot_acquire_coowner_attribution(tmp_path: Path) -> None:
    oracle = _shared_hook_oracle(tmp_path)
    _write_shared_hook_fixture(oracle, oracle.row.targets)
    oracle.hook_targets = oracle.row.targets
    oracle.assert_routing(oracle.row.targets)
    assert oracle.hook_coowner is not None
    name = ".claude/apm-hooks.json"
    path = oracle.roots["user"] / name
    payload = json.loads(path.read_text())
    for event, entries in oracle.hook_coowner.unowned_members[name].items():
        payload[event].extend({**entry, "_apm_source": "persistent-hooks"} for entry in entries)
    with pytest.raises(AssertionError, match="User hook acquired package ownership"):
        oracle.observe(
            "claim-user-hook",
            lambda: path.write_text(json.dumps(payload), encoding="ascii"),
            exact={"user": {name}},
        )


def test_real_actions_validate_coowner_before_credit() -> None:
    """A CLI action cannot silently bypass persistent source/target checks."""
    from tests.integration import test_primitive_target_covering_array as execution

    module = ast.parse(Path(execution.__file__).read_text(encoding="utf-8"))
    outer = next(
        (node for node in module.body if getattr(node, "name", "") == "_execute_row_body"), None
    )
    assert isinstance(outer, ast.FunctionDef), "Missing _execute_row_body action boundary"
    action = next((node for node in outer.body if getattr(node, "name", "") == "action"), None)
    assert isinstance(action, ast.FunctionDef), "Missing real CLI action function"
    calls = {
        node.func.attr: node
        for node in ast.walk(action)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"observe", "evaluated"} <= calls.keys(), "CLI action omitted observation or credit"
    assert "assert_hook_coowner_installed" in calls, "CLI action omitted co-owner validation"
    assert "hook_targets" in {keyword.arg for keyword in calls["observe"].keywords}
    assert calls["assert_hook_coowner_installed"].lineno < calls["evaluated"].lineno


@pytest.mark.parametrize(
    "row_id",
    ("claude-hooks-project", "interaction-963057b67e335224", "interaction-ad11ffadf32fe2bf"),
)
def test_hook_coowner_publication_uses_actual_row_transport_and_ref(
    tmp_path: Path, row_id: str
) -> None:
    """Exercise Git/pinned, Git/tag and local setup without changing scheduled rows."""
    from tests.integration.test_primitive_target_covering_array import _prepare_hook_coowner
    from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
    from tests.utils.lifecycle_interactions import INTERACTION_ROWS
    from tests.utils.local_git_repository import LocalGitRepositoryFactory
    from tests.utils.local_package import LocalPackageFactory

    row = next(row for row in (*ROUTING_ROWS, *INTERACTION_ROWS) if row.id == row_id)
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=dict(os.environ))
    setup = _prepare_hook_coowner(
        LocalPackageFactory(isolated.package_root),
        LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env()),
        row,
        row.targets,
        (),
    )
    if row.source_kind == "local":
        assert setup.declaration == {"path": setup.package.root.as_posix()}
        assert not setup.rewrites and not setup.commits
    else:
        assert len(setup.rewrites) == 1
        assert setup.declaration["type"] == "gitlab"
        assert setup.declaration["git"] == setup.rewrites[0][1]
        assert setup.declaration["ref"] == (
            "lifecycle-v1" if row.ref_state == "tag" else setup.commits[setup.package.name]
        )
        repository, _remote = setup.rewrites[0]
        assert (repository.origin / "objects").is_dir()
        if row.ref_state == "tag":
            assert (repository.worktree / ".git/refs/tags/lifecycle-v1").read_text().strip() == (
                setup.commits[setup.package.name]
            )


def _instruction_oracle(tmp_path: Path) -> tuple[InteractionOracle, _HookCoOwnerSetup]:
    """Create authored package inputs; only the real integrator generates the aggregate."""
    from tests.integration.test_primitive_target_covering_array import (
        _author,
        _bind_instruction_coowner,
        _prepare_instruction_coowner,
        _seed_unowned,
        _source_dependency,
    )
    from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
    from tests.utils.local_git_repository import LocalGitRepositoryFactory
    from tests.utils.local_package import LocalPackageFactory

    isolated = IsolatedApmEnvironment.create(
        tmp_path / "git-environment", base_env=dict(os.environ)
    )
    oracle = _oracle(tmp_path)
    oracle.row = next(row for row in ROUTING_ROWS if row.id == "copilot-instructions-user")
    oracle.deployment_root_id = "user"
    oracle.lock_root = oracle.roots["user"] / ".apm"
    factory = LocalPackageFactory(tmp_path / "sources")
    primary = factory.create(f"fixture-{oracle.row.id}", targets=oracle.row.targets)
    source = _author(factory, primary, oracle.row)
    dependency = _source_dependency(
        primary,
        remote=f"https://gitlab.example.invalid/apm-lifecycle/{primary.name}.git",
        parent=None,
    )
    oracle.sources = (replace(source, dependency_key=dependency.get_unique_key()),)
    setup = _prepare_instruction_coowner(
        factory,
        LocalGitRepositoryFactory(isolated.repository_root, env=isolated.subprocess_env()),
        oracle.row,
        oracle.row.targets,
    )
    oracle.instruction_coowner = _bind_instruction_coowner(setup, [dependency], oracle.lock_root)
    _seed_unowned(oracle, expected_routing(oracle.row, oracle.sources))
    return oracle, setup


def _materialize_instruction_state(
    oracle: InteractionOracle, setup: _HookCoOwnerSetup, *, primary: bool
) -> None:
    """Generate native content through the real integrator with an authored component lock."""
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.models.apm_package import APMPackage, PackageInfo
    from apm_cli.utils.yaml_io import dump_yaml

    coowner = oracle.instruction_coowner
    assert coowner is not None
    for path, content in coowner.materialized_files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    sources = (*oracle.sources, coowner.source) if primary else (coowner.source,)
    root = oracle.roots["user"]
    aggregate = root / ".copilot/copilot-instructions.md"
    aggregate.unlink(missing_ok=True)
    profile = KNOWN_TARGETS["copilot"].for_scope(user_scope=True)
    for source in sources:
        package_path = setup.package.root.parent / source.package_name
        package = APMPackage(
            name=source.package_name,
            version="0.1.0",
            package_path=package_path,
            source=source.dependency_key,
        )
        info = PackageInfo(
            package=package,
            install_path=package_path,
            resolved_reference=None,
            installed_at="2026-01-01T00:00:00",
        )
        result = InstructionIntegrator().integrate_instructions_for_target(profile, info, root)
        assert result.files_integrated == 1 and result.files_skipped == 0
    owners = tuple(source.dependency_key for source in sources)
    LockFile(
        dependencies={
            source.dependency_key: LockedDependency(
                repo_url=source.dependency_key,
                name=source.package_name,
                resolved_commit=setup.commits.get(source.package_name, "1" * 40),
            )
            for source in sources
        },
        deployment_ledger=DeploymentLedger(
            records={
                "aggregate": DeploymentRecord(
                    DeploymentLocator(
                        LocatorKind.PROJECT_RELATIVE,
                        "copilot",
                        ".copilot/copilot-instructions.md",
                        None,
                        "user",
                    ),
                    owners,
                    owners[-1],
                    None,
                )
            }
        ),
        _deployments_present=True,
    ).write(oracle.lock_root / "apm.lock.yaml")
    dump_yaml({"dependencies": {"apm": [setup.declaration]}}, oracle.lock_root / "apm.yml")


@pytest.mark.parametrize("ambient_identity", ("missing", "configured"))
def test_copilot_source_setup_uses_isolated_git_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambient_identity: str
) -> None:
    from tests.utils.local_git_repository import LocalGitRepositoryFactory

    ambient_config = tmp_path / "ambient.gitconfig"
    ambient_config.write_text("", encoding="ascii")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(ambient_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    expected = {
        "GIT_AUTHOR_NAME": "APM Test",
        "GIT_AUTHOR_EMAIL": "apm-test@example.invalid",
        "GIT_COMMITTER_NAME": "APM Test",
        "GIT_COMMITTER_EMAIL": "apm-test@example.invalid",
    }
    for name in expected:
        if ambient_identity == "missing":
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, "ambient@example.invalid")

    def require_isolation(root: Path, *, env: Mapping[str, str]) -> LocalGitRepositoryFactory:
        assert {name: env.get(name) for name in expected} == expected, (
            "Copilot source setup inherited the developer's Git identity"
        )
        assert env.get("GIT_CONFIG_GLOBAL") != str(ambient_config), (
            "Copilot source setup inherited the developer's Git configuration"
        )
        return LocalGitRepositoryFactory(root, env=env)

    monkeypatch.setattr(
        "tests.utils.local_git_repository.LocalGitRepositoryFactory", require_isolation
    )
    oracle, setup = _instruction_oracle(tmp_path)
    assert oracle.instruction_coowner is not None
    assert setup.declaration["ref"] == setup.commits[setup.package.name]


def test_copilot_seed_keeps_generated_aggregate_absent_and_notes_outside_writes(
    tmp_path: Path,
) -> None:
    from tests.integration.test_primitive_target_covering_array import _row_permissions

    oracle, setup = _instruction_oracle(tmp_path)
    expected = expected_routing(oracle.row, oracle.sources)
    assert not (oracle.roots["user"] / ".copilot/copilot-instructions.md").exists()
    notes = oracle.roots["user"] / ".copilot/lifecycle-user-notes.md"
    assert oracle.protected_text == {
        notes: b"# User notes\n\nKeep this independently authored foreign prose.\n"
    }
    exact, trees = _row_permissions(
        oracle.row, oracle.row, expected, [], oracle.lock_root, list(setup.rewrites)
    )
    assert ".copilot/lifecycle-user-notes.md" not in exact["user"]
    assert not trees.get("user")
    assert setup.declaration["ref"] == setup.commits[setup.package.name]
    assert setup.declaration["type"] == "gitlab"
    assert len(setup.rewrites) == 1
    assert setup.source.primitive == "instructions"


@pytest.mark.parametrize("boundary", ("transition", "known-gap-reinstall", "protected-content"))
def test_copilot_unowned_notes_remain_exact_at_every_boundary(
    tmp_path: Path, boundary: str
) -> None:
    from tests.integration.test_primitive_target_covering_array import _assert_reinstall

    oracle, setup = _instruction_oracle(tmp_path)
    _materialize_instruction_state(oracle, setup, primary=True)
    oracle.assert_routing(("copilot",))
    before = oracle.capture()
    _assert_reinstall(oracle, before, None)
    path = next(iter(oracle.protected_text))
    lock_before = (oracle.lock_root / "apm.lock.yaml").read_bytes()
    credit_before = list(oracle.evaluations)
    diagnostic = "outside its declared write set" if boundary == "transition" else "Unowned notes"
    with pytest.raises(AssertionError, match=diagnostic):
        if boundary == "transition":
            oracle.observe(
                "reinstall",
                lambda: path.write_bytes(b"modified user notes\n"),
                exact={"user": expected_routing(oracle.row, oracle.sources).files},
            )
        else:
            path.write_bytes(b"modified user notes\n")
            if boundary == "known-gap-reinstall":
                _assert_reinstall(oracle, before, None)
            else:
                oracle.assert_protected_content(oracle.capture())
    aggregate = oracle.roots["user"] / ".copilot/copilot-instructions.md"
    assert oracle.sources[0].marker in aggregate.read_text()
    assert oracle.instruction_coowner.body in aggregate.read_text()
    assert (oracle.lock_root / "apm.lock.yaml").read_bytes() == lock_before
    assert oracle.evaluations == credit_before


@pytest.mark.parametrize("boundary", ("transition", "known-gap-reinstall", "routing"))
def test_copilot_coowner_loss_cannot_earn_credit(tmp_path: Path, boundary: str) -> None:
    from tests.integration.test_primitive_target_covering_array import _assert_reinstall

    oracle, setup = _instruction_oracle(tmp_path)
    _materialize_instruction_state(oracle, setup, primary=True)
    oracle.assert_routing(("copilot",))
    before = oracle.capture()
    aggregate = oracle.roots["user"] / ".copilot/copilot-instructions.md"
    corrupted = aggregate.read_text().replace(oracle.instruction_coowner.body, "")
    lock_before = (oracle.lock_root / "apm.lock.yaml").read_bytes()
    credit_before = list(oracle.evaluations)
    with pytest.raises(AssertionError, match="Instruction co-owner contribution lost"):
        if boundary == "transition":
            oracle.observe(
                "reinstall",
                lambda: aggregate.write_text(corrupted),
                exact={"user": {".copilot/copilot-instructions.md"}},
            )
        else:
            aggregate.write_text(corrupted)
            if boundary == "known-gap-reinstall":
                _assert_reinstall(oracle, before, None)
            else:
                oracle.assert_routing(("copilot",))
    assert oracle.sources[0].marker in aggregate.read_text()
    assert (oracle.lock_root / "apm.lock.yaml").read_bytes() == lock_before
    assert oracle.evaluations == credit_before


@pytest.mark.parametrize("fault", ("removed-body", "unknown-identity", "lost-survivor"))
def test_copilot_uninstall_rejects_stale_body_or_false_identity(tmp_path: Path, fault: str) -> None:
    oracle, setup = _instruction_oracle(tmp_path)
    _materialize_instruction_state(oracle, setup, primary=False)
    oracle.assert_routing(())
    aggregate = oracle.roots["user"] / ".copilot/copilot-instructions.md"
    content = aggregate.read_text()
    coowner = oracle.instruction_coowner
    if fault == "removed-body":
        corrupted = content + f"\n# {oracle.sources[0].marker}\n"
        diagnostic = "Removed instruction source survived uninstall"
    elif fault == "unknown-identity":
        corrupted = content.replace(
            f"<!-- apm:source:{coowner.source.dependency_key} -->",
            "<!-- apm:source:unknown -->",
        )
        diagnostic = "Instruction survivor identity differs"
    else:
        corrupted = content.replace(coowner.body, "")
        diagnostic = "Instruction co-owner contribution lost"
    assert corrupted != content
    lock_before = (oracle.lock_root / "apm.lock.yaml").read_bytes()
    credit_before = list(oracle.evaluations)
    aggregate.write_text(corrupted)
    with pytest.raises(AssertionError, match=diagnostic):
        oracle.assert_routing(())
    assert (oracle.lock_root / "apm.lock.yaml").read_bytes() == lock_before
    assert oracle.evaluations == credit_before


def test_copilot_generated_aggregate_requires_all_authored_owners(tmp_path: Path) -> None:
    """Both real native contributions can survive while a false ledger drops an owner."""
    oracle, setup = _instruction_oracle(tmp_path)
    _materialize_instruction_state(oracle, setup, primary=True)
    oracle.assert_routing(("copilot",))
    lock = LockFile.read(oracle.lock_root / "apm.lock.yaml")
    assert len(lock.deployment_ledger.records) == 1
    key = next(iter(lock.deployment_ledger.records))
    record = lock.deployment_ledger.records[key]
    lock.deployment_ledger.records[key] = replace(record, owners=(record.active_owner,))
    lock.write(oracle.lock_root / "apm.lock.yaml")
    aggregate = oracle.roots["user"] / ".copilot/copilot-instructions.md"
    assert oracle.sources[0].marker in aggregate.read_text()
    assert oracle.instruction_coowner.body in aggregate.read_text()
    credit_before = list(oracle.evaluations)
    with pytest.raises(AssertionError, match="Ledger ownership differs from authored source"):
        oracle.assert_routing(("copilot",))
    assert oracle.evaluations == credit_before


@pytest.mark.parametrize(
    "fault", ("lock", "source", "manifest", "primary-materialization", "graph", "commit")
)
def test_copilot_survivor_requires_exact_authored_state(tmp_path: Path, fault: str) -> None:
    from apm_cli.utils.yaml_io import dump_yaml
    from tests.integration.test_primitive_target_covering_array import _assert_survivor_state

    oracle, setup = _instruction_oracle(tmp_path)
    _materialize_instruction_state(oracle, setup, primary=False)
    _assert_survivor_state(oracle, setup, setup.commits)
    if fault == "lock":
        lock = LockFile.read(oracle.lock_root / "apm.lock.yaml")
        lock.dependencies.clear()
        lock.write(oracle.lock_root / "apm.lock.yaml")
        check = oracle.assert_instruction_coowner_installed
        diagnostic = "Persistent instruction co-owner missing from lock"
    elif fault == "source":
        path = next(iter(oracle.instruction_coowner.materialized_files))
        path.write_bytes(b"altered source manifest\n")
        check = oracle.assert_instruction_coowner_installed
        diagnostic = "Persistent instruction co-owner source changed or missing"
    else:
        if fault == "manifest":
            dump_yaml({"dependencies": {"apm": []}}, oracle.lock_root / "apm.yml")
            diagnostic = "Uninstall must preserve exactly"
        elif fault == "primary-materialization":
            oracle.instruction_coowner.primary_materializations[0].mkdir(parents=True)
            diagnostic = "Removed instruction package materialization survived"
        else:
            lock = LockFile.read(oracle.lock_root / "apm.lock.yaml")
            if fault == "graph":
                lock.dependencies["unexpected/package"] = LockedDependency(
                    repo_url="unexpected/package",
                    name=setup.package.name,
                    resolved_commit=setup.commits[setup.package.name],
                )
                diagnostic = "Authored dependency graph differs from lock"
            else:
                lock.dependencies[setup.dependency.get_unique_key()].resolved_commit = "0" * 40
                diagnostic = "Authored Git commits differ from lock"
            lock.write(oracle.lock_root / "apm.lock.yaml")

        def check() -> None:
            _assert_survivor_state(oracle, setup, setup.commits)

    with pytest.raises(AssertionError, match=diagnostic):
        check()


def test_copilot_headerless_collision_preserves_bytes_without_deployment_claim(
    tmp_path: Path,
) -> None:
    """Positive native control: an unmanaged aggregate is a collision, not a merge input."""
    from apm_cli.integration.instruction_integrator import InstructionIntegrator
    from apm_cli.models.apm_package import APMPackage, PackageInfo

    oracle, setup = _instruction_oracle(tmp_path)
    aggregate = oracle.roots["user"] / ".copilot/copilot-instructions.md"
    original = b"# Genuine user-authored instructions\nDo not overwrite.\n"
    aggregate.write_bytes(original)
    package = APMPackage(
        name=setup.package.name,
        version="0.1.0",
        package_path=setup.package.root,
        source=setup.dependency.get_unique_key(),
    )
    info = PackageInfo(
        package=package,
        install_path=setup.package.root,
        resolved_reference=None,
        installed_at="2026-01-01T00:00:00",
    )
    result = InstructionIntegrator().integrate_instructions_for_target(
        KNOWN_TARGETS["copilot"].for_scope(user_scope=True), info, oracle.roots["user"]
    )
    assert aggregate.read_bytes() == original
    assert result.files_skipped == 1 and result.files_integrated == 0
    assert result.target_paths == [] and result.materializations == ()
    lock = LockFile.read(oracle.lock_root / "apm.lock.yaml")
    assert lock is None or not lock.deployment_ledger.records


def test_real_action_validates_instruction_survivor_before_credit() -> None:
    """A missing action guard must fail intentionally, not through a lookup exception."""
    from tests.integration import test_primitive_target_covering_array as execution

    module = ast.parse(Path(execution.__file__).read_text(encoding="utf-8"))
    outer = next(
        (node for node in module.body if getattr(node, "name", "") == "_execute_row_body"), None
    )
    assert isinstance(outer, ast.FunctionDef)
    action = next((node for node in outer.body if getattr(node, "name", "") == "action"), None)
    assert isinstance(action, ast.FunctionDef)
    calls = {
        node.func.attr: node
        for node in ast.walk(action)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "assert_instruction_coowner_installed" in calls, (
        "CLI action omitted instruction survivor validation"
    )
    assert "evaluated" in calls
    assert calls["assert_instruction_coowner_installed"].lineno < calls["evaluated"].lineno
