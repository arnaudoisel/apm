"""Exact byte capture and independent assessment copies for a single leaf."""

import hashlib
import json
import os
import shutil
import stat
from pathlib import Path

from ..utils.path_security import (
    PathTraversalError,
    ensure_path_within,
    has_symlink_component,
    validate_path_segments,
)
from .models import Artifact, BaselineSnapshot, ContractError, ContractLimits, FileEntry, LeafPlan
from .process import local_git


def _path(root: Path, name: str) -> Path:
    target = root / name
    try:
        validate_path_segments(name, reject_empty=True, context="contract snapshot")
        ensure_path_within(target, root)
    except PathTraversalError as exc:
        raise ContractError(
            f"Snapshot path leaves its allowed directory: {name}", code="unsafe_snapshot_path"
        ) from exc
    if has_symlink_component(root, target):
        raise ContractError(
            f"Snapshot path contains a symlink: {name}", code="unsafe_snapshot_path"
        )
    return target


def _read(root: Path, name: str, maximum: int) -> tuple[bytes, FileEntry]:
    """Bounded no-follow read with descriptor identity checked before and after."""
    path = _path(root, name)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            raise ContractError(
                f"Expected a bounded regular file: {name}", code="snapshot_file_limit"
            )
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())

    def identity(info: os.stat_result) -> tuple[int, int, int, int, int]:
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    if (
        len(data) > maximum
        or identity(before) != identity(after)
        or identity(after) != identity(path.stat(follow_symlinks=False))
    ):
        raise ContractError(f"File changed during capture: {name}", code="source_changed")
    entry = FileEntry(
        name, hashlib.sha256(data).hexdigest(), len(data), stat.S_IMODE(after.st_mode) & 0o777
    )
    return data, entry


def _digest(entries: tuple[FileEntry, ...]) -> str:
    content = [[item.relative_path, item.sha256, item.size, item.mode] for item in entries]
    return hashlib.sha256(json.dumps(content, separators=(",", ":")).encode("utf-8")).hexdigest()


def inspect_retained_log(run_directory: Path, maximum_bytes: int) -> FileEntry:
    """Read the finalized bounded transcript through the exact-byte authority."""
    return _read(run_directory, "transcript.log", maximum_bytes)[1]


def _git_marker(root: Path) -> Path | None:
    return next(
        (parent / ".git" for parent in (root, *root.parents) if (parent / ".git").exists()), None
    )


def _selected_names(plan: LeafPlan) -> tuple[str, ...]:
    root = plan.project_root
    names = set(plan.contract.needs)
    names.update((plan.contract.path.relative_to(root).as_posix(), "apm.yml"))
    for lock in ("apm.lock.yaml", "apm.lock"):
        if (root / lock).exists():
            names.add(lock)
    if _git_marker(root) is not None:
        for line in local_git(root, "ls-files", "--stage", "-z").split(b"\0"):
            if not line:
                continue
            metadata, encoded = line.split(b"\t", 1)
            name = encoded.decode("utf-8")
            if metadata.split()[0] == b"160000":
                raise ContractError("Git submodules are unsupported in captured baselines.")
            if name.split("/")[0] in {".apm", "apm_modules"}:
                continue
            path = _path(root, name)
            if path.exists():
                names.add(name)
            if len(names) > plan.limits.baseline_files:
                raise ContractError("Baseline file count exceeds the limit.", code="baseline_limit")
    checks = _path(root, "checks")
    if checks.exists():
        if not checks.is_dir():
            raise ContractError("The checks resource bundle must be a directory.")
        for count, path in enumerate(checks.rglob("*"), start=1):
            if count > plan.limits.resource_files * 2:
                raise ContractError(
                    "Check resource tree exceeds the entry limit.", code="resource_limit"
                )
            name = path.relative_to(root).as_posix()
            path = _path(root, name)
            if not path.is_dir():
                names.add(name)
    names.discard(plan.contract.produces)
    if len(names) > plan.limits.baseline_files:
        raise ContractError("Baseline file count exceeds the limit.", code="baseline_limit")
    if len({name.casefold() for name in names}) != len(names):
        raise ContractError("Case-colliding snapshot paths are unsupported.")
    return tuple(sorted(names))


def inspect_workspace(plan: LeafPlan) -> tuple[FileEntry, ...]:
    """Read effective tracked bytes and explicit untracked resources without writes."""
    entries = []
    total = resource_total = resource_count = 0
    for name in _selected_names(plan):
        _, entry = _read(plan.project_root, name, plan.limits.file_bytes)
        entries.append(entry)
        total += entry.size
        if name.startswith("checks/"):
            resource_total += entry.size
            resource_count += 1
        if total > plan.limits.baseline_bytes:
            raise ContractError("Baseline bytes exceed the limit.", code="baseline_limit")
        if (
            resource_total > plan.limits.resource_bytes
            or resource_count > plan.limits.resource_files
        ):
            raise ContractError("Check resources exceed the limit.", code="resource_limit")
    source = plan.contract.path.relative_to(plan.project_root).as_posix()
    expected = {source: plan.contract.source_digest, "apm.yml": plan.manifest_digest}
    if plan.lock_digest is not None:
        lock_name = (
            "apm.lock.yaml" if (plan.project_root / "apm.lock.yaml").exists() else "apm.lock"
        )
        expected[lock_name] = plan.lock_digest
    for entry in entries:
        digest = expected.get(entry.relative_path)
        if digest is not None and digest != entry.sha256:
            raise ContractError("Planned source identity changed. Plan again.", code="plan_changed")
    return tuple(entries)


def _write(root: Path, entry: FileEntry, data: bytes) -> None:
    destination = _path(root, entry.relative_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as target:
        target.write(data)
    destination.chmod(entry.mode)


def _copy_entries(source: Path, destination: Path, entries: tuple[FileEntry, ...]) -> None:
    for expected in entries:
        data, observed = _read(source, expected.relative_path, expected.size)
        if observed != expected:
            raise ContractError("Captured baseline identity changed.", code="baseline_changed")
        _write(destination, expected, data)


def _initialize_git(root: Path, template: Path) -> str:
    local_git(root, "init", "--quiet", f"--template={template}")
    info = root / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_text(
        "* -text -filter -ident -working-tree-encoding\n", encoding="ascii"
    )
    local_git(root, "add", "--all", "--force", "--", ".")
    local_git(root, "commit", "--quiet", "--allow-empty", "-m", "Captured contract baseline")
    return local_git(root, "rev-parse", "HEAD").decode("ascii").strip()


def capture_workspace(plan: LeafPlan, run_directory: Path) -> BaselineSnapshot:
    """Materialize the admitted effective tree and a fresh producer workspace."""
    entries = inspect_workspace(plan)
    baseline = run_directory / "baseline"
    producer = run_directory / "producer"
    baseline.mkdir(mode=0o700)
    producer.mkdir(mode=0o700)
    _copy_entries(plan.project_root, baseline, entries)
    _copy_entries(baseline, producer, entries)
    template = run_directory / "git-template"
    template.mkdir(mode=0o700)
    original_head = None
    if _git_marker(plan.project_root) is not None:
        refs = local_git(
            plan.project_root, "rev-parse", "--verify", "--quiet", "HEAD", accepted_codes=(0, 1)
        )
        original_head = refs.decode("ascii").strip() or None
    head = _initialize_git(baseline, template)
    shutil.copytree(baseline / ".git", producer / ".git")
    resources = tuple(entry for entry in entries if entry.relative_path.startswith("checks/"))
    return BaselineSnapshot(
        baseline, producer, entries, _digest(entries), original_head, head, _digest(resources)
    )


def capture_output(
    snapshot: BaselineSnapshot,
    declared_path: str,
    run_directory: Path,
    limits: ContractLimits,
) -> Artifact | None:
    """Capture only a newly produced file; absence remains absence, not an empty file."""
    target = _path(snapshot.producer, declared_path)
    if not target.exists():
        return None
    data, entry = _read(snapshot.producer, declared_path, limits.output_bytes)
    output_root = run_directory / "artifacts"
    output_root.mkdir(mode=0o700)
    _write(output_root, entry, data)
    captured = _path(output_root, declared_path)
    captured.chmod(0o400)
    return Artifact(declared_path, captured, entry.sha256, entry.size)


def prepare_check_workspace(
    snapshot: BaselineSnapshot,
    artifact: Artifact,
    run_directory: Path,
    check_name: str,
) -> Path:
    """Fresh baseline plus captured bytes; never apply candidate patches here."""
    parent = run_directory / "assessments"
    parent.mkdir(exist_ok=True, mode=0o700)
    root = _path(parent, check_name)
    root.mkdir(mode=0o700)
    _copy_entries(snapshot.root, root, snapshot.files)
    shutil.copytree(snapshot.root / ".git", root / ".git", symlinks=False)
    data, observed = _read(artifact.path.parent, artifact.path.name, artifact.size)
    if observed.sha256 != artifact.sha256 or observed.size != artifact.size:
        raise ContractError("Captured output identity changed.", code="artifact_changed")
    _write(root, FileEntry(artifact.relative_path, artifact.sha256, artifact.size, 0o400), data)
    return root


def verify_check_integrity(
    snapshot: BaselineSnapshot, artifact: Artifact, check_workspace: Path
) -> bool:
    """Check supplied identities; no claim against same-user mutation-and-restore."""
    try:
        _, captured = _read(artifact.path.parent, artifact.path.name, artifact.size)
        _, subject = _read(check_workspace, artifact.relative_path, artifact.size)
        if any(
            entry.sha256 != artifact.sha256 or entry.size != artifact.size
            for entry in (captured, subject)
        ):
            return False
        for expected in snapshot.files:
            if expected.relative_path.startswith("checks/"):
                _, observed = _read(check_workspace, expected.relative_path, expected.size)
                if observed != expected:
                    return False
        return True
    except (OSError, ContractError):
        return False
