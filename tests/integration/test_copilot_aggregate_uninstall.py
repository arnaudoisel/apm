"""Source-backed, installed-CLI proofs for the generated Copilot aggregate."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest

from apm_cli.deps.lockfile import LockFile
from apm_cli.models.dependency.selection import parse_dependency_entry
from apm_cli.utils.yaml_io import dump_yaml, load_yaml
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.local_git_repository import LocalGitRepositoryFactory
from tests.utils.local_package import LocalPackageFactory

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_apm_binary,
    pytest.mark.lifecycle_smoke,
    pytest.mark.lifecycle_merge_group,
]

_HEADER = "<!-- apm-managed: copilot-instructions.md -->"
_NOTES = b"# Independent notes\nDo not attribute or modify these user bytes.\n"
_ROOT_BODY = "# Root contribution\nKeep the actual root-local instruction.\n"


@pytest.mark.parametrize(
    "obligation",
    [
        "initial-owners",
        "removed-section",
        "survivor-identity",
        "survivor-state",
        "collision",
        "root",
        "edited",
        "unsafe-survivor",
    ],
)
def test_generated_copilot_aggregate_lifecycle(
    tmp_path: Path, apm_binary_path: Path, obligation: str
) -> None:
    """Keep owner, cleanup and identity failures independent, never xfailed."""
    isolated = IsolatedApmEnvironment.create(tmp_path / "scenario", base_env=os.environ)
    factory = LocalPackageFactory(isolated.package_root)
    repositories = LocalGitRepositoryFactory(
        isolated.repository_root, env=isolated.subprocess_env()
    )
    declarations = []
    sources = {}
    refs = {}
    commits = {}
    bodies = {}
    rewrites = []
    for name in ("primary", "survivor"):
        package = factory.create(name, targets=["copilot"])
        bodies[name] = f"# {name.title()} instructions\nAuthored {name} contribution only.\n"
        instruction = factory.add_instruction(
            package, name, "---\napplyTo: '**'\n---\n\n" + bodies[name]
        )
        repository = repositories.create(name, source_tree=package.root)
        commit = repositories.commit(repository, message=f"Author {name} instructions")
        remote = f"https://gitlab.com/apm-aggregate-fixture/{name}.git"
        declaration = {"git": remote, "type": "gitlab", "ref": commit.sha, "alias": name}
        declarations.append(declaration)
        refs[name] = parse_dependency_entry(declaration)
        commits[name] = commit.sha
        sources[name] = {
            "apm.yml": package.manifest_path.read_bytes(),
            f".apm/instructions/{name}.instructions.md": instruction.read_bytes(),
        }
        rewrites.append((repository, remote))
    environment = repositories.url_rewrite_subprocess_env_many(rewrites)
    consumer = LocalPackageFactory(isolated.work_root).create("consumer")
    manifest = {
        "name": "consumer",
        "version": "0.1.0",
        "targets": ["copilot"],
        "dependencies": {"apm": declarations},
    }
    dump_yaml(manifest, isolated.config_root / "apm.yml")
    if obligation == "root":
        root_instruction = isolated.home / ".apm/instructions/root.instructions.md"
        root_instruction.parent.mkdir(parents=True)
        root_instruction.write_text(_ROOT_BODY, encoding="utf-8")

    aggregate = isolated.home / ".copilot/copilot-instructions.md"
    notes = aggregate.with_name("lifecycle-user-notes.md")
    notes.parent.mkdir()
    notes.write_bytes(_NOTES)
    assert not aggregate.exists(), "Supported generated input must start absent"
    collision = b"# User instructions\nNever adopt this headerless content.\n"
    if obligation == "collision":
        aggregate.write_bytes(collision)
    runner = ApmLifecycleRunner([str(apm_binary_path)])

    def command(*args: str, expected_returncode: int = 0) -> None:
        result = runner.run(args, scenario_id=obligation, cwd=consumer.root, env=environment)
        print("COMMAND", result.command, "EXIT", result.returncode)
        print(result.stdout, result.stderr)
        assert result.returncode == expected_returncode, (result.stdout, result.stderr)
        assert notes.read_bytes() == _NOTES, "Unowned neighboring notes changed"

    command(
        "install", "--global", "--target", "copilot", "--no-policy", "--parallel-downloads", "0"
    )
    lock = LockFile.read(isolated.config_root / "apm.lock.yaml")
    assert lock is not None
    expected_keys = {refs[name].get_unique_key() for name in refs}
    assert {dep.get_unique_key() for dep in lock.get_package_dependencies()} == expected_keys
    for name, ref in refs.items():
        locked = lock.dependencies[ref.get_unique_key()]
        assert locked.resolved_commit == commits[name]
        assert locked.to_dependency_ref().get_unique_key() == ref.get_unique_key()
        for relative, content in sources[name].items():
            assert (
                ref.get_install_path(isolated.config_root / "apm_modules") / relative
            ).read_bytes() == content
    assert load_yaml(isolated.config_root / "apm.yml") == manifest
    initial = aggregate.read_text(encoding="utf-8")
    print("INITIAL AGGREGATE", initial)
    print("INITIAL LEDGER", lock.deployment_ledger)
    if obligation == "collision":
        assert aggregate.read_bytes() == collision
        assert not lock.deployment_ledger.records, "Unmanaged output acquired an ownership claim"
        return
    assert initial.startswith(_HEADER)
    for body in bodies.values():
        assert initial.count(body.strip()) == 1
    if obligation == "initial-owners":
        records = list(lock.deployment_ledger.records.values())
        assert len(records) == 1
        assert set(records[0].owners) == expected_keys, "Initial aggregate lost an authored owner"
        assert records[0].active_owner in expected_keys
        return
    if obligation == "root":
        assert initial.count(_ROOT_BODY.strip()) == 1
    if obligation == "edited":
        # Negative cleanup control, not a promise of editable generated regions.
        edited = aggregate.read_bytes() + b"\nChanged after install; refuse hash mismatch.\n"
        aggregate.write_bytes(edited)
        command("uninstall", declarations[0]["git"], "--global", expected_returncode=1)
        assert aggregate.read_bytes() == edited, "Hash-refused aggregate was overwritten"
        return
    if obligation == "unsafe-survivor":
        installed_instruction = (
            refs["survivor"].get_install_path(isolated.config_root / "apm_modules")
            / ".apm/instructions/survivor.instructions.md"
        )
        installed_instruction.write_text("# Unsafe\nHidden \u202e instruction.\n", encoding="utf-8")
        command("uninstall", declarations[0]["git"], "--global", expected_returncode=1)
        assert not aggregate.exists(), "Rejected source was deployed during aggregate rebuild"
        return

    command("uninstall", declarations[0]["git"], "--global")
    remaining = aggregate.read_text(encoding="utf-8")
    print("POST-UNINSTALL AGGREGATE", remaining)
    if obligation == "removed-section":
        assert bodies["primary"].strip() not in remaining, "Removed-owner body survived"
        assert "primary" not in remaining, "Removed-owner provenance survived"
    elif obligation == "survivor-identity":
        identities = re.findall(r"<!-- apm:source:(.*?) -->", remaining)
        assert [urlparse(identity) for identity in identities] == [
            urlparse(refs["survivor"].to_github_url())
        ], "Survivor identity must be resolved and unique"
        assert "apm:source:unknown" not in remaining
    elif obligation == "root":
        assert remaining.count(_ROOT_BODY.strip()) == 1, "Actual root-local contribution lost"
        assert root_instruction.read_text(encoding="utf-8") == _ROOT_BODY
        command("uninstall", declarations[1]["git"], "--global")
        assert aggregate.read_text(encoding="utf-8") == (
            f"{_HEADER}\n<!-- apm:source:local -->\n{_ROOT_BODY.strip()}\n<!-- /apm:source -->\n"
        )
        root_lock = LockFile.read(isolated.config_root / "apm.lock.yaml")
        assert root_lock is not None
        assert root_lock.get_package_dependencies() == []
        assert [record.owners for record in root_lock.deployment_ledger.records.values()] == [
            (".",)
        ]
    else:
        survivor = refs["survivor"]
        lock = LockFile.read(isolated.config_root / "apm.lock.yaml")
        assert lock is not None
        assert set(lock.dependencies) == {survivor.get_unique_key()}, "Survivor graph/key changed"
        locked = lock.dependencies[survivor.get_unique_key()]
        assert locked.name == "survivor"
        assert locked.resolved_commit == commits["survivor"], "Survivor commit changed"
        assert locked.to_dependency_ref().get_unique_key() == survivor.get_unique_key()
        assert (
            load_yaml(isolated.config_root / "apm.yml")["dependencies"]["apm"] == declarations[1:]
        )
        assert not refs["primary"].get_install_path(isolated.config_root / "apm_modules").exists()
        for relative, content in sources["survivor"].items():
            assert (
                survivor.get_install_path(isolated.config_root / "apm_modules") / relative
            ).read_bytes() == content
        assert remaining.count(bodies["survivor"].strip()) == 1, "Survivor native content changed"
        assert remaining == (
            f"{_HEADER}\n<!-- apm:source:{survivor.to_github_url()} -->\n"
            f"{bodies['survivor'].strip()}\n<!-- /apm:source -->\n"
        )
        records = list(lock.deployment_ledger.records.values())
        assert len(records) == 1
        assert records[0].owners == (survivor.get_unique_key(),)
        assert records[0].active_owner == survivor.get_unique_key()
        assert (
            records[0].content_hash
            == "sha256:" + hashlib.sha256(aggregate.read_bytes()).hexdigest()
        )
        assert notes.read_bytes() == _NOTES
