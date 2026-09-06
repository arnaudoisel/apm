"""Small source-derived routing expectations and open-world transition assertions."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TypeVar

from apm_cli.core.deployment_ledger import DeploymentLedgerCodec
from apm_cli.deps.lockfile import LockFile
from apm_cli.integration.targets import KNOWN_TARGETS
from apm_cli.utils.path_security import ensure_path_within, has_symlink_component
from tests.utils.artifact_snapshot import (
    ArtifactEntry,
    ArtifactSnapshotSet,
    assert_snapshot_changes_within,
    assert_snapshot_set_unchanged,
)
from tests.utils.lifecycle_interactions import RoutingRow

T = TypeVar("T")


def ancestors(paths: Iterable[str]) -> frozenset[str]:
    """Permit directory entries, never all descendants of an ancestor."""
    return frozenset(
        parent.as_posix()
        for path in paths
        for parent in PurePosixPath(path).parents
        if parent.as_posix() != "."
    )


@dataclass(frozen=True)
class SourceFixture:
    """Authored input identity; no field comes from a product output ledger."""

    package_name: str
    primitive: str
    name: str
    marker: str
    source_files: tuple[str, ...]
    dependency_key: str


@dataclass(frozen=True)
class RoutingExpectation:
    """Exact native files and their independently expected ledger owners."""

    files: frozenset[str]
    ledger: Mapping[tuple[str, str], tuple[str, ...]]
    shared: frozenset[str] = frozenset()


@dataclass(frozen=True)
class HookCoOwner:
    """A declared survivor, with native slices and bytes authored before install."""

    source: SourceFixture
    members: Mapping[str, Mapping[str, object]]
    materialized_files: Mapping[Path, bytes]
    unowned_members: Mapping[str, Mapping[str, object]]


@dataclass(frozen=True)
class InstructionCoOwner:
    """An ordinary installed contributor to a generated, not user-editable, aggregate."""

    source: SourceFixture
    body: str
    materialized_files: Mapping[Path, bytes]
    primary_materializations: tuple[Path, ...]


def expected_routing(
    row: RoutingRow,
    sources: tuple[SourceFixture, ...],
    targets: tuple[str, ...] | None = None,
) -> RoutingExpectation:
    """Map the tiny fixture layout through catalog capabilities, not integrators."""
    files: set[str] = set()
    ledger: dict[tuple[str, str], tuple[str, ...]] = {}
    shared: set[str] = set()

    def claim(target: str, path: str, source: SourceFixture) -> None:
        key = (target, path)
        owners = ledger.get(key, ())
        assert source.dependency_key, "Source fixture omitted authored dependency identity"
        ledger[key] = (
            *(owner for owner in owners if owner != source.dependency_key),
            source.dependency_key,
        )

    for target in row.targets if targets is None else targets:
        profile = KNOWN_TARGETS[target].for_scope(user_scope=row.user_scope)
        assert profile is not None
        for source in sources:
            kind = source.primitive
            mapping = profile.primitives.get(kind)
            # A prompt fixture can also be rendered as a command on widening.
            if mapping is None and kind == "prompts":
                mapping = profile.primitives.get("commands")
            if mapping is None:
                continue
            base = PurePosixPath(mapping.deploy_root or profile.root_dir) / mapping.subdir
            owned: set[str]
            if kind == "skills":
                owned = {(base / source.name / "SKILL.md").as_posix()}
                claim(profile.name, (base / source.name).as_posix(), source)
            elif kind == "canvas":
                owned = {
                    (base / source.name / suffix).as_posix()
                    for suffix in ("extension.mjs", "assets/info.txt")
                }
            elif mapping.format_id == "copilot_user_instructions":
                owned = {(base / "copilot-instructions.md").as_posix()}
                shared.update(owned)
            elif mapping.format_id == "grok_rules":
                owned = {(base / f"{source.name}.instructions.md").as_posix()}
            elif kind == "hooks" and profile.hooks_config_display:
                config = (
                    PurePosixPath(profile.root_dir)
                    / PurePosixPath(profile.hooks_config_display).name
                ).as_posix()
                sidecar = (PurePosixPath(profile.root_dir) / "apm-hooks.json").as_posix()
                files.update((config, sidecar))
                shared.update((config, sidecar))
                continue
            elif kind == "hooks":
                suffix = "-pretooluse-1" if mapping.format_id == "kiro_hooks" else ""
                owned = {(base / f"{source.package_name}-{source.name}{suffix}.json").as_posix()}
            else:
                owned = {(base / f"{source.name}{mapping.extension}").as_posix()}
            files.update(owned)
            for path in owned:
                claim(profile.name, path, source)
    return RoutingExpectation(frozenset(files), ledger, frozenset(shared))


def assert_members_preserved(expected: object, actual: object, *, path: str = "$") -> None:
    """Protect foreign nested configuration values inside a writable shared file."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"Shared configuration replaced at {path}"
        for key, value in expected.items():
            assert key in actual, f"Shared configuration member removed: {path}.{key}"
            assert_members_preserved(value, actual[key], path=f"{path}.{key}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"Shared configuration array replaced at {path}"
        assert all(item in actual for item in expected), f"Shared ownership lost at {path}"
    else:
        assert actual == expected, f"Shared configuration member overwritten: {path}"


@dataclass
class InteractionOracle:
    """One mandatory observation boundary with lifetime native-path ownership."""

    roots: Mapping[str, Path]
    deployment_root_id: str
    lock_root: Path
    sources: tuple[SourceFixture, ...]
    row: RoutingRow
    protected_json: dict[Path, object] = field(default_factory=dict)
    protected_text: dict[Path, bytes] = field(default_factory=dict)
    hook_coowner: HookCoOwner | None = None
    hook_targets: tuple[str, ...] = ()
    instruction_coowner: InstructionCoOwner | None = None
    instruction_active: bool = False
    introduced: set[str] = field(default_factory=set)
    operations: list[str] = field(default_factory=list)
    evaluations: list[tuple[str, tuple[str, ...]]] = field(default_factory=list)
    pending: str | None = None

    def capture(self) -> ArtifactSnapshotSet:
        """Observe complete roots, including unrelated target neighbors."""
        return ArtifactSnapshotSet.capture(self.roots)

    def _native_path(
        self,
        name: str,
        entries: Mapping[str, ArtifactEntry],
        *,
        required: bool = True,
    ) -> Path:
        """Validate native fixture leaves before any live content read.

        Cache/materialization links are not native outputs and are deliberately
        outside this regular-file rule.
        """
        root = self.roots[self.deployment_root_id]
        path = ensure_path_within(root / name, root)
        entry = entries.get(name)
        assert entry is not None or not required, f"Missing required deployment: {name}"
        assert (entry is None or entry.kind == "file") and not has_symlink_component(
            root, root / name
        ), f"Native fixture leaf is not a regular file: {name}"
        return path

    def assert_protected_content(
        self,
        snapshot: ArtifactSnapshotSet,
        *,
        hook_targets: tuple[str, ...] | None = None,
    ) -> None:
        """Protect authored shared members even on unchanged or known-gap actions."""
        entries = {
            entry.relative_path: entry
            for entry in snapshot.snapshot(self.deployment_root_id).entries
        }
        root = self.roots[self.deployment_root_id]
        for protected in (self.protected_json, self.protected_text):
            for path, expected in protected.items():
                native = self._native_path(path.relative_to(root).as_posix(), entries)
                if isinstance(expected, bytes):
                    assert expected == native.read_bytes(), f"Unowned notes changed: {path}"
                else:
                    assert_members_preserved(
                        expected, json.loads(native.read_text(encoding="utf-8"))
                    )
        if self.hook_coowner is not None:
            authorized = self.hook_targets if hook_targets is None else hook_targets
            for target, members in self.hook_coowner.members.items():
                for name, expected in members.items():
                    native = self._native_path(name, entries, required=target in authorized)
                    if target in authorized:
                        assert_members_preserved(
                            expected, json.loads(native.read_text(encoding="utf-8"))
                        )
                    elif name in entries:
                        content = native.read_text(encoding="utf-8")
                        source = self.hook_coowner.source
                        assert (
                            source.marker not in content and source.package_name not in content
                        ), f"Deauthorized co-owner hook survived: {name}"
            for name, events in self.hook_coowner.unowned_members.items():
                if name not in entries:
                    continue
                native = self._native_path(name, entries)
                sidecar = json.loads(native.read_text(encoding="utf-8"))
                for event, unowned in events.items():
                    assert all(
                        {key: value for key, value in member.items() if key != "_apm_source"}
                        not in unowned
                        for member in sidecar.get(event, ())
                    ), f"User hook acquired package ownership: {name}"
        if self.instruction_active:
            self.assert_instruction_content(snapshot)

    def assert_instruction_content(
        self, snapshot: ArtifactSnapshotSet, *, after_uninstall: bool = False
    ) -> None:
        """Require source-authored contributions, and only the survivor after uninstall."""
        coowner = self.instruction_coowner
        if coowner is None:
            return
        entries = {
            entry.relative_path: entry
            for entry in snapshot.snapshot(self.deployment_root_id).entries
        }
        paths = expected_routing(self.row, (coowner.source,)).files
        for name in paths:
            content = self._native_path(name, entries).read_text(encoding="utf-8")
            assert coowner.body in content, f"Instruction co-owner contribution lost: {name}"
            if after_uninstall:
                assert all(
                    source.marker not in content
                    and source.package_name not in content
                    and source.dependency_key not in content
                    for source in self.sources
                ), f"Removed instruction source survived uninstall: {name}"
                identities = re.findall(r"<!-- apm:source:([^>\n]*?) -->", content)
                assert identities == [coowner.source.dependency_key], (
                    f"Instruction survivor identity differs from authored source: {identities!r}"
                )

    def assert_instruction_coowner_installed(self) -> None:
        """Validate the survivor's lock membership and immutable authored materialization."""
        coowner = self.instruction_coowner
        if coowner is None:
            return
        lock = LockFile.read(self.lock_root / "apm.lock.yaml")
        assert lock is not None and coowner.source.dependency_key in lock.dependencies, (
            "Persistent instruction co-owner missing from lock"
        )
        for path, expected in coowner.materialized_files.items():
            assert path.is_file() and path.read_bytes() == expected, (
                f"Persistent instruction co-owner source changed or missing: {path}"
            )

    def assert_hook_coowner_installed(self) -> None:
        """Reject removal or rewriting of the surviving package's authored inputs."""
        if self.hook_coowner is None:
            return
        lock = LockFile.read(self.lock_root / "apm.lock.yaml")
        key = self.hook_coowner.source.dependency_key
        assert lock is not None and key in lock.dependencies, (
            "Persistent hook co-owner missing from lock"
        )
        for path, expected in self.hook_coowner.materialized_files.items():
            assert path.is_file() and path.read_bytes() == expected, (
                f"Persistent hook co-owner source changed or missing: {path}"
            )

    def observe(
        self,
        operation: str,
        action: Callable[[], T],
        *,
        exact: Mapping[str, Iterable[str]] | None = None,
        trees: Mapping[str, Iterable[str]] | None = None,
        unchanged: bool = False,
        hook_targets: tuple[str, ...] | None = None,
    ) -> T:
        """Observe every CLI/fixture action and fail closed on forgotten evaluation."""
        with self.transition(
            operation, exact=exact, trees=trees, unchanged=unchanged, hook_targets=hook_targets
        ):
            return action()

    @contextmanager
    def transition(
        self,
        operation: str,
        *,
        exact: Mapping[str, Iterable[str]] | None = None,
        trees: Mapping[str, Iterable[str]] | None = None,
        unchanged: bool = False,
        hook_targets: tuple[str, ...] | None = None,
    ) -> Iterator[None]:
        """Observe compound fixture setup with the same mandatory CLI boundary."""
        assert self.pending is None, f"Skipped transition assertions: {self.pending}"
        before = self.capture()
        try:
            yield
        finally:
            after = self.capture()
        if unchanged:
            assert_snapshot_set_unchanged(before, after)
        else:
            exact = exact or {}
            trees = trees or {}
            allowed = {
                root_id: frozenset(exact.get(root_id, ()))
                | ancestors((*exact.get(root_id, ()), *trees.get(root_id, ())))
                for root_id in self.roots
            }
            assert_snapshot_changes_within(before, after, exact_paths=allowed, tree_prefixes=trees)
            for root_id, snapshot in after.snapshots:
                directories = ancestors((*exact.get(root_id, ()), *trees.get(root_id, ())))
                assert all(
                    entry.kind == "directory"
                    for entry in snapshot.entries
                    if entry.relative_path in directories
                ), f"Writable ancestor is not a directory: {root_id}"
        entries = {
            entry.relative_path: entry for entry in after.snapshot(self.deployment_root_id).entries
        }
        lifetime_targets = tuple(
            dict.fromkeys((*self.row.targets, *self.row.widen_targets, *self.row.narrow_targets))
        )
        native_files = expected_routing(self.row, self.sources, lifetime_targets).files
        for name in native_files:
            self._native_path(name, entries, required=False)
        self.assert_protected_content(after, hook_targets=hook_targets)
        if hook_targets is not None:
            self.hook_targets = hook_targets
        self.operations.append(operation)
        self.pending = operation

    def evaluated(self, *laws: str) -> None:
        """Record laws only after their corresponding assertions returned."""
        assert self.pending is not None, "Evaluation without an observed transition"
        self.evaluations.append(
            (
                self.pending,
                ("filesystem.open_world_observation", "ownership.preserve_unowned", *laws),
            )
        )
        self.pending = None

    def assert_routing(self, targets: tuple[str, ...]) -> None:
        """Require every expected file/claim and reject extra claims and stale routes."""
        expected = expected_routing(self.row, self.sources, targets)
        if self.instruction_coowner is not None:
            # The independent instruction package remains authorized on primary uninstall.
            sources = self.sources if targets else ()
            expected = expected_routing(
                self.row,
                (*sources, self.instruction_coowner.source),
                targets or self.row.targets,
            )
        root = self.roots[self.deployment_root_id]
        snapshot = self.capture()
        entries = {
            entry.relative_path: entry
            for entry in snapshot.snapshot(self.deployment_root_id).entries
        }
        contents_by_path = {}
        for name in expected.files:
            assert name in entries, f"Missing required deployment: {name}"
            path = self._native_path(name, entries)
            contents_by_path[name] = path.read_text(encoding="utf-8")
            assert contents_by_path[name], f"Missing required deployment: {name}"
        lock = LockFile.read(self.lock_root / "apm.lock.yaml")
        records = () if lock is None else tuple(lock.deployment_ledger.records.values())
        observed = {(record.locator.target, record.locator.value) for record in records}
        assert observed == set(expected.ledger), (
            "Ledger differs from source-derived routing: "
            f"missing={set(expected.ledger) - observed}, "
            f"unexpected={observed - set(expected.ledger)}"
        )
        assert len(records) == len(expected.ledger), "Duplicate source-derived ledger claim"
        for record in records:
            locator = record.locator
            owners = expected.ledger[(locator.target, locator.value)]
            assert record.owners == owners and record.active_owner == owners[-1], (
                f"Ledger ownership differs from authored source: {locator.value}: "
                f"expected={owners!r}, observed={record.owners!r}/{record.active_owner!r}"
            )
            # These static native fixtures use the legacy path compatibility
            # encoding. Non-Copilot user paths still encode scope as project;
            # filesystem authorization above is independently rooted in HOME.
            assert (
                locator.kind == "project-relative"
                and locator.scope == DeploymentLedgerCodec.legacy_scope(locator.value)
                and locator.runtime is None
            ), f"Ledger locator metadata differs from source fixture: {locator!r}"
        active_paths = set(expected.files) | {path for _target, path in expected.ledger}
        possible_paths: set[str] = set(self.introduced)
        possible_shared: set[str] = set(expected.shared)
        for name, base in KNOWN_TARGETS.items():
            if (
                base.user_root_resolver is not None
                or base.for_scope(user_scope=self.row.user_scope) is None
            ):
                continue
            alternative = expected_routing(self.row, self.sources, (name,))
            possible_paths.update(alternative.files)
            possible_shared.update(alternative.shared)
        # Include never-authorized and previously-widened outputs, not just initial claims.
        for name in possible_paths - active_paths:
            path = root / name
            if name in possible_shared and (path.exists() or path.is_symlink()):
                path = self._native_path(name, entries)
                contents = path.read_text(encoding="utf-8")
                assert all(
                    source.package_name not in contents and source.marker not in contents
                    for source in self.sources
                ), f"Removed target leaked shared ownership: {name}"
            else:
                assert not (path.exists() or path.is_symlink()), (
                    f"Removed target leaked deployment: {name}"
                )
        if targets:
            assert lock is not None, "Successful materialization omitted lockfile"
            for source in self.sources:
                source_paths = expected_routing(self.row, (source,), targets).files
                assert source_paths, f"Source has no authorized route: {source.package_name}"
                for name in source_paths:
                    contents = contents_by_path[name]
                    marker = (
                        source.package_name if name.endswith("apm-hooks.json") else source.marker
                    )
                    assert marker in contents, (
                        f"Required source content not deployed: {source.package_name}: {name}"
                    )
                    if source.marker.endswith("version-b") and not name.endswith("apm-hooks.json"):
                        assert (
                            source.marker.removesuffix("version-b") + "version-a" not in contents
                        ), f"Stale ref content survived update: {name}"
        self.assert_instruction_content(snapshot, after_uninstall=not targets)
        if self.instruction_coowner is not None:
            self.instruction_active = True
        self.assert_protected_content(snapshot)
        self.introduced.update(active_paths)

    def assert_finished(self, required_operations: Iterable[str]) -> None:
        """Reject skipped intermediate assertions or absent required transitions."""
        assert self.pending is None, f"Skipped transition assertions: {self.pending}"
        missing = set(required_operations) - {name for name, _laws in self.evaluations}
        assert not missing, f"Missing evaluated transitions: {sorted(missing)}"
