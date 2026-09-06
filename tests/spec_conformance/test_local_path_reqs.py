"""Executable req-mf-016 source anchoring, admission, and containment contracts."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from apm_cli.core.scope import InstallScope
from apm_cli.deps.apm_resolver import APMDependencyResolver
from apm_cli.deps.tiered_ref_resolver import RefFreshnessPolicy
from apm_cli.install.context import InstallContext
from apm_cli.install.package_resolution import user_scope_rejection_reason
from apm_cli.install.phases.local_content import _copy_local_package
from apm_cli.install.phases.resolve import _materialization, _resolve_dependencies
from apm_cli.install.resolution_staging import ResolutionStagingSession
from apm_cli.install.sources import LocalDependencySource
from apm_cli.models.apm_package import APMPackage
from apm_cli.models.dependency import DependencyReference
from apm_cli.utils.diagnostics import DiagnosticCollector
from apm_cli.utils.path_security import PathTraversalError
from apm_cli.utils.yaml_io import dump_yaml
from tests.spec_conformance._helpers import load_yaml_fixture
from tests.utils.local_package import LocalPackageFactory

pytestmark = pytest.mark.component


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize(
    "reference", ["./child", "../child", "/child", "~/child", ".\\child", "..\\child", "~\\child"]
)
def test_local_path_prefixes_are_recognized(reference: str) -> None:
    """Recognize local spelling without confusing it with a remote coordinate."""
    dep = DependencyReference.parse(reference)
    assert dep.is_local
    assert dep.local_path == reference


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("scope", [InstallScope.PROJECT, InstallScope.USER])
def test_local_sibling_uses_original_source_in_each_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: InstallScope
) -> None:
    """Both real admission consumers retain the original parent, not CWD/staging."""
    factory = LocalPackageFactory(tmp_path / "sources")
    child = factory.create("child", targets=["cursor"])
    source = factory.add_command(child, "child", "---\ndescription: child\n---\nOriginal child\n")
    parent = factory.create("parent")
    dump_yaml(load_yaml_fixture("manifest", "valid-local-parent.yml"), parent.manifest_path)
    consumer = LocalPackageFactory(tmp_path / "scope").create(
        "consumer", dependencies=[{"path": parent.root.as_posix()}]
    )
    unrelated = tmp_path / "unrelated"
    decoy = LocalPackageFactory(unrelated).create("child")
    (unrelated / "cwd").mkdir()
    monkeypatch.chdir(unrelated / "cwd")
    package = APMPackage.from_apm_yml(consumer.manifest_path, source_path=consumer.root)
    modules = consumer.root / "apm_modules"
    modules.mkdir()
    ctx = InstallContext(
        project_root=consumer.root,
        apm_dir=consumer.root,
        apm_package=package,
        scope=scope,
        all_apm_deps=package.get_apm_dependencies(),
        apm_modules_dir=modules,
        ref_freshness_policy=RefFreshnessPolicy.REPRODUCIBLE,
        downloader=MagicMock(shared_clone_cache=None),
        diagnostics=DiagnosticCollector(),
    )
    staging = ResolutionStagingSession(modules)
    try:
        _resolve_dependencies(ctx, staging, _materialization.CachedMaterializationPathReader())
        assert not ctx.callback_failures
        assert {dep.repo_url for dep in ctx.deps_to_install} == {"_local/parent", "_local/child"}
        child_ref = next(dep for dep in ctx.deps_to_install if dep.repo_url == "_local/child")
        node = ctx.dependency_graph.dependency_tree.get_node(child_ref.get_unique_key())
        assert node.parent.package.source_path == parent.root
        assert node.package.source_path == child.root
        assert not child.root.is_relative_to(consumer.root)
        assert decoy.manifest_path.is_file()
        assert child_ref.local_path == "../child"
        assert child_ref.declaring_parent == parent.root.as_posix()
        assert child_ref.anchored_local_path == child.root.as_posix()
        assert child_ref.get_unique_key() in ctx.callback_downloaded
        materialized = LocalDependencySource(
            ctx, child_ref, child_ref.get_install_path(modules), child_ref.get_unique_key()
        ).acquire()
        assert materialized is not None
        assert materialized.package_info.package.source_path == child.root
        assert (
            child_ref.get_install_path(modules)
            .joinpath(".apm", "prompts", "child.prompt.md")
            .read_bytes()
            == source.read_bytes()
        )
        ctx.downloader.download_package.assert_not_called()
    finally:
        staging.rollback()


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize(
    "context", ["direct", "missing-parent", "missing-source", "relative-source", "remote"]
)
def test_user_relative_admission_requires_proven_local_parent(tmp_path: Path, context: str) -> None:
    """Existing files and a claimed anchor alone cannot authorize a global read."""
    child = LocalPackageFactory(tmp_path).create("child")
    dep = DependencyReference.parse("./child")
    if context != "direct":
        dep.declaring_parent = tmp_path.as_posix()
    parent = APMPackage(
        name="parent",
        version="1.0.0",
        source="org/remote" if context == "remote" else "_local/parent",
        source_path=(
            None
            if context == "missing-source"
            else Path("relative")
            if context == "relative-source"
            else tmp_path
        ),
    )
    reason = user_scope_rejection_reason(
        dep, InstallScope.USER, parent_pkg=None if context == "missing-parent" else parent
    )
    assert child.manifest_path.is_file()
    assert reason is not None
    assert "relative local paths" in reason
    assert "absolute path" in reason
    assert (
        user_scope_rejection_reason(
            DependencyReference.parse(child.root.as_posix()), InstallScope.USER
        )
        is None
    )


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("case", ["project-relative", "user-absolute", "user-home"])
def test_direct_local_source_is_anchored_before_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Absolute/home sources work globally; project-relative sources use the project."""
    package = LocalPackageFactory(tmp_path / "sources").create("package")
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    reference = {
        "project-relative": "../sources/package",
        "user-absolute": package.root.as_posix(),
        "user-home": "~/sources/package",
    }[case]
    scope = InstallScope.PROJECT if case == "project-relative" else InstallScope.USER
    dep = DependencyReference.parse(reference)
    assert user_scope_rejection_reason(dep, scope) is None
    destination = consumer / "apm_modules" / "_local" / "package"
    assert (
        _copy_local_package(dep, destination, consumer, project_root=consumer, logger=None)
        == destination
    )
    assert (destination / "apm.yml").read_bytes() == package.manifest_path.read_bytes()


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("reference", ["../child", "..\\child"])
def test_remote_relative_child_retains_repository_and_ref(tmp_path: Path, reference: str) -> None:
    """A sibling inside the remote repository remains remote, not a host-file read."""
    modules = tmp_path / "apm_modules"
    factory = LocalPackageFactory(modules / "repo" / "packages")
    parent = factory.create("parent")
    child = factory.create("child")
    parent_dep = DependencyReference.parse_from_dict(
        {
            "git": "https://gitlab.example.invalid:8443/org/repo",
            "path": "packages/parent",
            "ref": "a" * 40,
        }
    )
    parent_pkg = APMPackage.from_apm_yml(parent.manifest_path, source_path=parent.root)
    parent_pkg.source = parent_dep.repo_url
    resolver = APMDependencyResolver(apm_modules_dir=modules)
    expanded = resolver._expand_or_reject_remote_parent_local_path(
        parent_dep, parent_pkg, DependencyReference.parse(reference)
    )
    assert child.manifest_path.is_file()
    assert expanded is not None
    assert not expanded.is_local
    assert expanded.local_path is None
    assert expanded.virtual_path == "packages/child"
    assert (
        expanded.host,
        expanded.port,
        expanded.repo_url,
        expanded.reference,
        expanded.explicit_scheme,
    ) == (
        parent_dep.host,
        parent_dep.port,
        parent_dep.repo_url,
        parent_dep.reference,
        parent_dep.explicit_scheme,
    )
    assert not resolver._rejected_remote_local_keys


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize(
    "kind", ["escape", "absolute", "absolute-inside", "home", "windows-absolute", "symlink"]
)
def test_remote_parent_cannot_expand_into_host_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """The real expansion and loader gates refuse remote-to-host transitions."""
    modules = tmp_path / "apm_modules"
    parent = LocalPackageFactory(modules / "repo" / "packages").create("parent")
    outside = LocalPackageFactory(tmp_path).create("outside")
    parent_dep = DependencyReference.parse_from_dict(
        {"git": "https://gitlab.example.invalid/org/repo", "path": "packages/parent", "ref": "v1"}
    )
    parent_pkg = APMPackage.from_apm_yml(parent.manifest_path, source_path=parent.root)
    parent_pkg.source = parent_dep.repo_url
    paths = {
        "escape": "../../../../outside",
        "absolute": outside.root.as_posix(),
        "absolute-inside": parent.root.as_posix(),
        "home": "~/outside",
        "windows-absolute": "C:/outside",
        "symlink": "../linked",
    }
    if kind == "symlink":
        (parent.root.parent / "linked").symlink_to(outside.root, target_is_directory=True)
    dep = DependencyReference.parse_from_dict({"path": paths[kind]})
    callback = MagicMock(side_effect=AssertionError("Rejected remote path reached acquisition"))
    resolver = APMDependencyResolver(apm_modules_dir=modules, download_callback=callback)
    assert resolver._expand_or_reject_remote_parent_local_path(parent_dep, parent_pkg, dep) is None
    assert dep.get_unique_key() in resolver._rejected_remote_local_keys
    assert resolver._try_load_dependency_package(dep, parent_pkg=parent_pkg) is None
    callback.assert_not_called()
    assert outside.manifest_path.is_file()
    assert not (modules / "_local").exists()
    assert paths[kind] in "".join(capsys.readouterr().out.split())


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize(
    "selected_alias", [False, True], ids=["source-directory", "resolved-source-alias"]
)
def test_selected_local_root_allows_only_internal_symlink_content(
    tmp_path: Path, selected_alias: bool
) -> None:
    """Selecting a source-directory alias is distinct from following its contents."""
    package = LocalPackageFactory(tmp_path / "sources").create("package")
    target = package.root / "content.txt"
    target.write_text("Internal content\n", encoding="ascii")
    (package.root / "link.txt").symlink_to("content.txt")
    selected = package.root
    if selected_alias:
        selected = tmp_path / "chosen-package"
        selected.symlink_to(package.root, target_is_directory=True)
    consumer = tmp_path / "consumer"
    destination = consumer / "apm_modules" / "_local" / "package"
    result = _copy_local_package(
        DependencyReference.parse(selected.as_posix()),
        destination,
        consumer,
        project_root=consumer,
        logger=None,
    )
    assert result == destination
    assert (destination / "link.txt").read_bytes() == target.read_bytes()
    assert not (destination / "link.txt").is_symlink()
    assert (destination / "content.txt").read_bytes() == target.read_bytes()


@pytest.mark.req("req-mf-016")
@pytest.mark.parametrize("kind", ["outside-file", "outside-directory", "broken", "directory-cycle"])
def test_local_package_rejects_uncontained_or_unresolvable_symlink(
    tmp_path: Path, kind: str
) -> None:
    """A selected local source cannot import external or invalid link content."""
    package = LocalPackageFactory(tmp_path / "sources").create("package")
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "not-package-content.txt"
    secret.write_text("Outside content\n", encoding="ascii")
    targets = {
        "outside-file": secret,
        "outside-directory": outside,
        "broken": package.root / "missing",
        "directory-cycle": package.root,
    }
    link = package.root / "linked"
    link.symlink_to(
        targets[kind], target_is_directory=kind in {"outside-directory", "directory-cycle"}
    )
    destination = tmp_path / "consumer" / "apm_modules" / "_local" / "package"
    with pytest.raises(PathTraversalError) as error:
        _copy_local_package(
            DependencyReference.parse(package.root.as_posix()),
            destination,
            tmp_path / "consumer",
            project_root=tmp_path / "consumer",
            logger=None,
        )
    assert "linked" in str(error.value)
    assert not destination.exists()
    assert secret.read_text(encoding="ascii") == "Outside content\n"


@pytest.mark.req("req-mf-016")
def test_local_package_rejects_file_symlink_cycle(tmp_path: Path) -> None:
    """An OS-detected file cycle also fails; no whole-install rollback is promised."""
    package = LocalPackageFactory(tmp_path / "sources").create("package")
    (package.root / "linked").symlink_to("linked")
    destination = tmp_path / "consumer" / "apm_modules" / "_local" / "package"
    # pathlib reports a cycle as RuntimeError on Python 3.12; newer versions
    # use OSError, which the copier translates to PathTraversalError.
    with pytest.raises((PathTraversalError, RuntimeError), match="linked"):
        _copy_local_package(
            DependencyReference.parse(package.root.as_posix()),
            destination,
            tmp_path / "consumer",
            project_root=tmp_path / "consumer",
            logger=None,
        )
    assert not (destination / "linked").exists()
