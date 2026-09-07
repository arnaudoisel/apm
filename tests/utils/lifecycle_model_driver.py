"""One observed action boundary for deterministic and generated Phase 1 cases."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner, CommandResult
from tests.utils.artifact_snapshot import (
    ArtifactEntry,
    ArtifactSnapshotSet,
    assert_snapshot_changes_within,
    assert_snapshot_set_unchanged,
)
from tests.utils.lifecycle_model import (
    FAILED_COMMAND,
    FIXTURE_ID,
    IDEMPOTENCY,
    LAWS,
    OPEN_WORLD,
    OUTCOME,
    OWNERSHIP,
    READ_ONLY,
    ROUTING,
    SOURCE,
    LifecycleModel,
    ReplayCase,
    advance,
    applicable_laws,
    validate_case,
)

SKILL_NAME = "model-skill"
PACKAGE_NAME = "model-kit"
SKILL_PATH = f".agents/skills/{SKILL_NAME}/SKILL.md"
CACHE_ROOT = f"apm_modules/{PACKAGE_NAME}"
CACHE_SKILL_PATH = f"{CACHE_ROOT}/skills/{SKILL_NAME}/SKILL.md"
SOURCE_ROOT = f"apm_modules/apm-fixture-org/{PACKAGE_NAME}"
SOURCE_SKILL_PATH = f"{SOURCE_ROOT}/skills/{SKILL_NAME}/SKILL.md"
SKILL_BYTES = (
    f"---\nname: {SKILL_NAME}\n"
    f"description: Required lifecycle fixture skill {SKILL_NAME}\n---\n# {SKILL_NAME}\n"
).encode("ascii")
TAMPER_BYTES = b"# user tamper\n"
INSTALL_ARGS = ("install", "--no-policy", "--parallel-downloads", "0")
AUDIT_ARGS = ("audit", "--ci", "--no-policy", "--no-fail-fast", "--format", "json")
SENTINELS = (
    ("project", ".github/workflows/unrelated.yml", b"name: unrelated\n"),
    ("project", ".agents/skills/user-skill/SKILL.md", b"# User-owned skill\n"),
    ("user", ".apm/unrelated.txt", b"Unrelated user data\n"),
    ("user", ".config/unrelated-app/settings.json", b'{"user": true}\n'),
)

# Reviewed permissions for this exact fixture, not product selector/ledger output.
_PROJECT_DIRECTORIES = frozenset(
    {
        ".agents",
        ".agents/skills",
        f".agents/skills/{SKILL_NAME}",
        ".github",
        "apm_modules",
        "apm_modules/apm-fixture-org",
    }
)
_USER_DIRECTORIES = frozenset({".local", ".local/state", ".local/state/gh"})
_EXACT_WRITES = {
    "project": _PROJECT_DIRECTORIES | {SKILL_PATH, ".gitignore", "apm.lock.yaml"},
    "user": _USER_DIRECTORIES | {".apm/config.json", ".local/state/gh/device-id"},
}
_TREE_WRITES = {"project": frozenset({CACHE_ROOT, SOURCE_ROOT})}
_COMMANDS = {
    "install": INSTALL_ARGS,
    "reinstall": INSTALL_ARGS,
    "repair": INSTALL_ARGS,
    "dry_run": (*INSTALL_ARGS, "--dry-run"),
    "audit_clean": AUDIT_ARGS,
    "audit_tampered": AUDIT_ARGS,
    "prune_removed": ("prune",),
}


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _entry(snapshot: ArtifactSnapshotSet, root_id: str, path: str) -> ArtifactEntry | None:
    return next(
        (entry for entry in snapshot.snapshot(root_id).entries if entry.relative_path == path),
        None,
    )


@dataclass(frozen=True)
class ObservedState:
    """Real full-root capture plus raw manifest and lockfile facts, not predictions."""

    artifacts: ArtifactSnapshotSet
    declarations: tuple[str, ...]
    lock_commits: tuple[str | None, ...]
    cache_revision: str | None


def observe(roots: Mapping[str, Path]) -> ObservedState:
    """Capture all project/home paths, independently of product deployment claims."""
    artifacts = ArtifactSnapshotSet.capture(roots)
    manifest = yaml.safe_load((roots["project"] / "apm.yml").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Fixture manifest is not a mapping")
    dependencies = manifest.get("dependencies", {}).get("apm", [])
    if not isinstance(dependencies, list):
        raise ValueError("Fixture dependency declarations are not a list")
    lock_path = roots["project"] / "apm.lock.yaml"
    lock = yaml.safe_load(lock_path.read_text(encoding="utf-8")) if lock_path.exists() else {}
    if not isinstance(lock, dict):
        raise ValueError("Fixture lockfile is not a mapping")
    pin_path = roots["project"] / SOURCE_ROOT / ".apm-pin"
    pin = json.loads(pin_path.read_text(encoding="ascii")) if pin_path.exists() else {}
    if not isinstance(pin, dict):
        raise ValueError("Fixture cache pin is not a mapping")
    return ObservedState(
        artifacts=artifacts,
        declarations=tuple(json.dumps(value, sort_keys=True) for value in dependencies),
        lock_commits=tuple(value.get("resolved_commit") for value in lock.get("dependencies", [])),
        cache_revision=pin.get("resolved_commit"),
    )


@dataclass(frozen=True)
class TransitionObservation:
    """An actual action result paired with independently computed intent."""

    before: ObservedState
    after: ObservedState
    expected: LifecycleModel
    transition: str
    result: CommandResult | None
    dependency: str
    source_revision: str


def law_open_world(observation: TransitionObservation) -> None:
    """No unexplained write, root replacement, or ancestor-directory conversion."""
    before, after = observation.before.artifacts, observation.after.artifacts
    assert tuple(name for name, _ in before.snapshots) == ("project", "user")
    assert tuple(name for name, _ in after.snapshots) == ("project", "user")
    for root_id, captured in before.snapshots:
        current = after.snapshot(root_id)
        assert captured.root == current.root and captured.root_existed == current.root_existed
    transition = observation.transition
    if transition in {"dry_run", "audit_clean", "audit_tampered", "reinstall"}:
        assert_snapshot_set_unchanged(before, after)
        return
    exact = _EXACT_WRITES
    trees = _TREE_WRITES
    if transition in {"remove_declaration", "readd_declaration", "tamper"}:
        exact = {"project": {"apm.yml" if transition != "tamper" else SKILL_PATH}}
        trees = {}
    assert_snapshot_changes_within(before, after, exact_paths=exact, tree_prefixes=trees)
    for root_id, paths in (("project", _PROJECT_DIRECTORIES), ("user", _USER_DIRECTORIES)):
        for path in paths:
            entry = _entry(after, root_id, path)
            assert entry is None or entry.kind == "directory", (root_id, path, entry)


def law_preserve_unowned(observation: TransitionObservation) -> None:
    """Protect pre-existing neighbors inside both authorized and unrelated roots."""
    for root_id, path, content in SENTINELS:
        expected = ArtifactEntry(path, "file", _digest(content))
        assert _entry(observation.before.artifacts, root_id, path) == expected, path
        assert _entry(observation.after.artifacts, root_id, path) == expected, path


def law_authorized_targets(observation: TransitionObservation) -> None:
    """Locate fixture skill artifacts across entire roots, not just ledger paths."""
    expected = observation.expected
    skill = _entry(observation.after.artifacts, "project", SKILL_PATH)
    assert (skill is not None) is expected.materialized, skill
    authorized = {
        ("project", SKILL_PATH),
        ("project", CACHE_SKILL_PATH),
        ("project", SOURCE_SKILL_PATH),
    }
    for root_id, snapshot in observation.after.artifacts.snapshots:
        for entry in snapshot.entries:
            is_fixture = entry.kind != "directory" and (
                SKILL_NAME in Path(entry.relative_path).parts
                or entry.fingerprint == _digest(SKILL_BYTES)
            )
            if is_fixture:
                assert (root_id, entry.relative_path) in authorized, (root_id, entry)


def law_source_coherent(observation: TransitionObservation) -> None:
    """The pinned revision, cached skill, and clean deployment must agree."""
    assert observation.expected.materialized
    assert observation.after.lock_commits == (observation.source_revision,)
    assert observation.after.cache_revision == observation.source_revision
    for path in (CACHE_SKILL_PATH, SOURCE_SKILL_PATH):
        cached = _entry(observation.after.artifacts, "project", path)
        assert cached == ArtifactEntry(path, "file", _digest(SKILL_BYTES)), cached
    if observation.expected.clean:
        deployed = _entry(observation.after.artifacts, "project", SKILL_PATH)
        assert deployed == ArtifactEntry(SKILL_PATH, "file", _digest(SKILL_BYTES)), deployed


def assert_ci_audit_result(
    result: CommandResult,
    *,
    clean: bool,
    deployment_root: Path | None = None,
    tampered_path: Path | None = None,
) -> None:
    """Validate --ci outcomes; attributed consumers require complete replay.

    Model and interaction drivers, including filesystem fakes, supply their
    authored deployment root and tampered leaf. Neither driver may earn credit
    through the unattributed status-only helper path.
    This is a test receipt check, not an implementation of bare-audit policy.
    """
    assert type(result.returncode) is int and result.returncode == (0 if clean else 1), (
        f"audit: exit={result.returncode}\n{result.stdout}\n{result.stderr}"
    )
    try:
        payload = json.loads(result.stdout)
    except (ValueError, TypeError) as error:
        raise AssertionError(f"audit: invalid JSON result: {result.stdout!r}") from error
    assert isinstance(payload, dict) and payload.get("passed") is clean, (
        f"audit: unexpected passed value: {payload!r}"
    )
    if deployment_root is None:
        assert tampered_path is None, "audit: attribution requires a deployment root"
        return
    assert (tampered_path is None) is clean, "audit: missing or unexpected tamper input"
    checks = payload.get("checks")
    assert isinstance(checks, list) and checks, "audit: missing completed checks"
    by_name = {}
    for check in checks:
        assert isinstance(check, dict), f"audit: malformed check: {check!r}"
        name = check.get("name")
        assert isinstance(name, str) and name and name not in by_name, (
            f"audit: missing or duplicate check name: {name!r}"
        )
        assert type(check.get("passed")) is bool, f"audit: nonboolean check: {name}"
        assert isinstance(check.get("message"), str), f"audit: missing check message: {name}"
        details = check.get("details")
        assert isinstance(details, list) and all(isinstance(item, str) for item in details), (
            f"audit: malformed check details: {name}"
        )
        by_name[name] = check
    required = {
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
    }
    assert required <= by_name.keys(), (
        f"audit: incomplete verification: missing={sorted(required - by_name.keys())}"
    )
    failed = {name for name, check in by_name.items() if not check["passed"]}
    expected_failures = set() if clean else {"content-integrity", "drift"}
    assert failed <= expected_failures, f"audit: unrelated failed checks: {sorted(failed)}"
    assert by_name["drift"]["passed"] is clean, "audit: replay did not verify expected state"
    summary = payload.get("summary")
    assert isinstance(summary, dict) and all(
        type(summary.get(name)) is int and summary[name] == count
        for name, count in (
            ("total", len(checks)),
            ("passed", len(checks) - len(failed)),
            ("failed", len(failed)),
        )
    ), f"audit: inconsistent check summary: {summary!r}"
    drift = payload.get("drift")
    findings = drift.get("drift") if isinstance(drift, dict) else None
    assert isinstance(findings, list), "audit: missing completed replay findings"
    if clean:
        assert by_name["drift"]["message"] == "no drift detected against lockfile", (
            f"audit: clean replay was not completed: {by_name['drift']!r}"
        )
        assert not findings, f"audit: clean receipt contains drift: {findings!r}"
        return
    assert tampered_path is not None
    relative = tampered_path.relative_to(deployment_root).as_posix()
    # Replay may report a root-relative path or an absolute user-scope path.
    allowed_paths = {relative, tampered_path.as_posix()}
    assert len(findings) == 1 and isinstance(findings[0], dict), (
        f"audit: expected exactly the tampered fixture finding: {findings!r}"
    )
    finding = findings[0]
    inline_diff = finding.get("inline_diff")
    assert inline_diff is None or (
        isinstance(inline_diff, str) and not inline_diff.startswith("(read error:")
    ), f"audit: replay could not compare the tampered fixture: {finding!r}"
    assert (
        finding.get("kind") == "modified"
        and isinstance(finding.get("path"), str)
        and finding["path"] in allowed_paths
    ), f"audit: unrelated or incomplete integrity finding: {finding!r}"
    assert by_name["drift"]["details"] == [f"modified: {finding['path']}"], (
        "audit: replay check and findings disagree"
    )
    if "content-integrity" in failed:
        details = by_name["content-integrity"]["details"]
        assert len(details) == 1 and any(
            details[0].startswith(f"hash-drift: {path} (dep=") for path in allowed_paths
        ), f"audit: unrelated content-integrity failure: {details!r}"


def law_outcome(observation: TransitionObservation) -> None:
    """Expected manifest, deployment, lock, status, and audit/dry-run diagnostics."""
    expected = observation.expected
    assert observation.after.declarations == (
        (observation.dependency,) if expected.declared else ()
    )
    lock = _entry(observation.after.artifacts, "project", "apm.lock.yaml")
    assert (lock is not None) is expected.locked, lock
    skill = _entry(observation.after.artifacts, "project", SKILL_PATH)
    if expected.materialized:
        content = SKILL_BYTES if expected.clean else TAMPER_BYTES
        assert skill == ArtifactEntry(SKILL_PATH, "file", _digest(content)), skill
    else:
        assert skill is None, skill
    result = observation.result
    if observation.transition not in _COMMANDS:
        assert result is None
        return
    assert result is not None, "Missing command result"
    if observation.transition in {"audit_clean", "audit_tampered"}:
        project_root = observation.before.artifacts.snapshot("project").root
        clean = observation.transition == "audit_clean"
        assert_ci_audit_result(
            result,
            clean=clean,
            deployment_root=project_root,
            tampered_path=None if clean else project_root / SKILL_PATH,
        )
    else:
        assert result.returncode == 0
    if observation.transition == "dry_run":
        assert "[i] APM dependencies (1):" in result.stdout.splitlines()


def law_idempotency(observation: TransitionObservation) -> None:
    """A converged reinstall preserves bytes everywhere under both roots."""
    assert observation.transition == "reinstall"
    assert_snapshot_set_unchanged(observation.before.artifacts, observation.after.artifacts)


def law_failed_command(observation: TransitionObservation) -> None:
    """A failing tamper audit is read-only; this does not claim install rollback."""
    assert observation.transition == "audit_tampered"
    assert observation.result is not None and observation.result.returncode == 1
    assert_snapshot_set_unchanged(observation.before.artifacts, observation.after.artifacts)


def law_read_only(observation: TransitionObservation) -> None:
    """Successful planning and clean observation cannot change durable state."""
    assert observation.transition in {"dry_run", "audit_clean"}
    assert_snapshot_set_unchanged(observation.before.artifacts, observation.after.artifacts)


LAW_CHECKS: dict[str, Callable[[TransitionObservation], None]] = {
    OPEN_WORLD: law_open_world,
    OWNERSHIP: law_preserve_unowned,
    ROUTING: law_authorized_targets,
    SOURCE: law_source_coherent,
    OUTCOME: law_outcome,
    IDEMPOTENCY: law_idempotency,
    FAILED_COMMAND: law_failed_command,
    READ_ONLY: law_read_only,
}


class LifecycleDriver:
    """Observe, act, evaluate, then credit; all searches and replays share this path."""

    def __init__(
        self,
        *,
        roots: Mapping[str, Path],
        case_id: str,
        dependency: Mapping[str, object],
        source_revision: str,
        run_command: Callable[[tuple[str, ...], str], CommandResult],
        set_declared: Callable[[bool], None],
    ) -> None:
        self.roots = dict(roots)
        self.case_id = case_id
        self.dependency = json.dumps(dict(dependency), sort_keys=True)
        self.source_revision = source_revision
        self.run_command = run_command
        self.set_declared = set_declared
        self.state = LifecycleModel()
        self.sequence: list[str] = []
        self.fired: list[dict[str, object]] = []
        self.evaluations: list[dict[str, object]] = []
        self.failure: str | None = None

    def _fail(self, law: str, error: Exception, result: CommandResult | None = None) -> None:
        report = {
            "case_id": self.case_id,
            "fixture": FIXTURE_ID,
            "sequence": self.sequence,
            "law": law,
            "error": str(error),
        }
        if result is not None:
            report["command"] = list(result.command)
            report["returncode"] = result.returncode
            report["stdout"] = result.stdout
            report["stderr"] = result.stderr
        self.failure = json.dumps(report, sort_keys=True)
        raise AssertionError(self.failure) from error

    def apply(self, transition: str) -> None:
        """The only transition entrypoint, including tamper and declaration edits."""
        self.sequence.append(transition)
        result = None
        stage = "transition.legality"
        try:
            expected = advance(self.state, transition)
            required = applicable_laws(self.state, transition)
            stage = "accounting.law_registry"
            assert set(LAW_CHECKS) == LAWS, "Missing or unexpected executable laws"
            stage = "observation.capture_before"
            before = observe(self.roots)
            stage = "action.execute"
            if transition in _COMMANDS:
                result = self.run_command(_COMMANDS[transition], self.case_id)
            elif transition == "tamper":
                (self.roots["project"] / SKILL_PATH).write_bytes(TAMPER_BYTES)
            else:
                self.set_declared(transition == "readd_declaration")
            self.fired.append({"step": len(self.sequence), "transition": transition})
            stage = "observation.capture_after"
            observation = TransitionObservation(
                before,
                observe(self.roots),
                expected,
                transition,
                result,
                self.dependency,
                self.source_revision,
            )
            for law in sorted(required):
                stage = law
                self.evaluations.append(
                    {
                        "step": len(self.sequence),
                        "transition": transition,
                        "law": law,
                        "passed": False,
                    }
                )
                LAW_CHECKS[law](observation)
                self.evaluations[-1]["passed"] = True
            self.state = expected
        except Exception as error:
            self._fail(stage, error, result)

    def replay(self, case: ReplayCase) -> None:
        """Replay literal JSON operations and verify actual, not advertised, witnesses."""
        validate_case(case)
        assert self.case_id == case.case_id and not self.sequence
        for transition in case.sequence:
            self.apply(transition)
        assert tuple(row["transition"] for row in self.fired) == case.sequence
        assert {row["law"] for row in self.evaluations if row["passed"]} == case.expected_laws

    def evidence(self) -> dict[str, object]:
        """Return serializable evaluated witnesses before fixture teardown."""
        return {
            "schema_version": 1,
            "case_id": self.case_id,
            "fixture": FIXTURE_ID,
            "sequence": list(self.sequence),
            "transitions": [dict(row) for row in self.fired],
            "laws": [dict(row) for row in self.evaluations],
            "status": "failed" if self.failure else ("passed" if self.fired else "not_executed"),
            "failure": self.failure,
        }


def create_cli_driver(
    root: Path, apm_binary_path: Path, case_id: str
) -> tuple[LifecycleDriver, ApmLifecycleRunner]:
    """Reuse existing hermetic Git fixtures without importing them in fast contracts."""
    from tests.integration.test_required_lifecycle_state_machine import _new_scenario, _publish

    try:
        scenario = _new_scenario(root, apm_binary_path)
        published = _publish(scenario, PACKAGE_NAME, skill=SKILL_NAME)
        project = scenario.consumers.create(
            "model-consumer", dependencies=(published.dependency,), targets=("copilot",)
        )
        roots = {"project": project.root, "user": scenario.isolated.home}
        for root_id, path, content in SENTINELS:
            destination = roots[root_id] / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    except Exception as error:
        raise AssertionError(
            json.dumps(
                {
                    "case_id": case_id,
                    "fixture": FIXTURE_ID,
                    "sequence": [],
                    "law": "fixture.setup",
                    "error": str(error),
                },
                sort_keys=True,
            )
        ) from error

    def run_command(args: tuple[str, ...], identity: str) -> CommandResult:
        return scenario.runner.run(
            args, cwd=project.root, env=published.environment, scenario_id=identity
        )

    def set_declared(declared: bool) -> None:
        scenario.consumers.replace_apm_dependencies(
            project, (published.dependency,) if declared else ()
        )

    return LifecycleDriver(
        roots=roots,
        case_id=case_id,
        dependency=published.dependency,
        source_revision=published.commit.sha,
        run_command=run_command,
        set_declared=set_declared,
    ), scenario.runner
