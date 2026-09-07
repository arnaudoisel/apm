"""Cross-platform proofs for the candidate-only Windows installer gate."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import sys
import zipfile
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener

import pytest

from tests.utils.windows_installer_candidate import (
    ASSET_NAME,
    EXECUTABLE_MEMBER,
    InstallerArchive,
    preserve_windows_user_path,
    serve_installer_candidate,
)

pytestmark = [pytest.mark.component, pytest.mark.windows_compat]
ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "scripts/windows/test-install-script.ps1"
PWSH = shutil.which("pwsh")


@pytest.fixture
def candidate_environment(tmp_path: Path) -> dict[str, str]:
    """Create a ZIP without executing it; the native suite owns runtime identity."""
    archive = tmp_path / ASSET_NAME
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr(EXECUTABLE_MEMBER, b"MZ-test-candidate")
    return {
        "APM_CANDIDATE_ARCHIVE": str(archive),
        "APM_CANDIDATE_VERSION": "v9.8.7",
        "APM_CANDIDATE_SHA256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }


@pytest.mark.parametrize("role", ["CANDIDATE", "BASELINE"])
@pytest.mark.parametrize(
    ("version", "tag"),
    [("9.8.7", "v9.8.7"), ("v9.8.7", "v9.8.7"), ("9.8.7rc1", "v9.8.7rc1")],
)
def test_fixture_accepts_packager_metadata_version(
    candidate_environment: dict[str, str], role: str, version: str, tag: str
) -> None:
    """The packager's unprefixed version and historical source tags share one contract."""
    environment = {
        key.replace("CANDIDATE", role): value for key, value in candidate_environment.items()
    }
    environment[f"APM_{role}_VERSION"] = version
    archive = InstallerArchive.from_environment(environment, role)
    assert archive.version == tag
    assert archive.sha256 == candidate_environment["APM_CANDIDATE_SHA256"]


@pytest.mark.parametrize("field", ["ARCHIVE", "VERSION", "SHA256"])
def test_candidate_requires_every_explicit_input(
    candidate_environment: dict[str, str], field: str
) -> None:
    """Neither missing metadata nor an absent candidate may select a public tag."""
    candidate_environment.pop(f"APM_CANDIDATE_{field}")
    with pytest.raises(ValueError, match="no published-release fallback"):
        InstallerArchive.from_environment(candidate_environment, "CANDIDATE")


def test_missing_candidate_file_fails(candidate_environment: dict[str, str]) -> None:
    """A stale workflow path must fail before PowerShell or the server starts."""
    Path(candidate_environment["APM_CANDIDATE_ARCHIVE"]).unlink()
    with pytest.raises(FileNotFoundError):
        InstallerArchive.from_environment(candidate_environment, "CANDIDATE")


@pytest.mark.parametrize("version", ["v9.8", "@v9.8.7", "v9.8.7/other", "v9.8.7\n", "latest"])
def test_candidate_rejects_nonexact_tag(
    candidate_environment: dict[str, str], version: str
) -> None:
    """Fixture route identities may not smuggle paths or select moving tags."""
    candidate_environment["APM_CANDIDATE_VERSION"] = version
    with pytest.raises(ValueError, match=r"exact vX\.Y\.Z"):
        InstallerArchive.from_environment(candidate_environment, "CANDIDATE")


@pytest.mark.parametrize("digest", ["0" * 64, "not-a-digest"])
def test_candidate_rejects_wrong_digest(candidate_environment: dict[str, str], digest: str) -> None:
    """A checksum mismatch cannot be replaced with a locally computed trust value."""
    candidate_environment["APM_CANDIDATE_SHA256"] = digest
    with pytest.raises(ValueError, match="SHA256"):
        InstallerArchive.from_environment(candidate_environment, "CANDIDATE")


def test_candidate_rejects_tampered_archive(candidate_environment: dict[str, str]) -> None:
    """The caller's digest binds the served bytes, not just the archive's name."""
    with Path(candidate_environment["APM_CANDIDATE_ARCHIVE"]).open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="archive SHA256 mismatch"):
        InstallerArchive.from_environment(candidate_environment, "CANDIDATE")


def test_candidate_requires_native_bundle(candidate_environment: dict[str, str]) -> None:
    """An authenticated archive without the Windows executable cannot pass."""
    archive = Path(candidate_environment["APM_CANDIDATE_ARCHIVE"])
    with zipfile.ZipFile(archive, "w") as target:
        target.writestr("apm-linux-x86_64/apm", b"wrong platform")
    candidate_environment["APM_CANDIDATE_SHA256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="must contain exactly one"):
        InstallerArchive.from_environment(candidate_environment, "CANDIDATE")


def test_mirror_serves_only_verified_candidate_and_current_installer(
    candidate_environment: dict[str, str], tmp_path: Path
) -> None:
    """Production download paths serve immutable bytes with original checksums."""
    candidate = InstallerArchive.from_environment(candidate_environment, "CANDIDATE")
    baseline = replace(candidate, version="v9.8.6")
    installer = tmp_path / "install.ps1"
    installer.write_bytes(b"# exact checkout installer\r\n")
    opener = build_opener(ProxyHandler({}))
    # Replacement after verification cannot change the immutable fixture bytes.
    Path(candidate_environment["APM_CANDIDATE_ARCHIVE"]).write_bytes(b"replaced")
    with serve_installer_candidate(candidate, baseline, installer) as mirror:
        parsed = urlparse(mirror.base_url)
        assert (parsed.scheme, parsed.hostname, parsed.path) == ("http", "127.0.0.1", "")
        assert parsed.port is not None
        for release in (candidate, baseline):
            route = f"/releases/{release.version}/{ASSET_NAME}"
            with opener.open(mirror.base_url + route, timeout=5) as response:
                assert response.read() == release.payload
            with opener.open(mirror.base_url + route + ".sha256", timeout=5) as response:
                assert response.read() == release.checksum_bytes()
        with opener.open(mirror.base_url + "/installer/install.ps1", timeout=5) as response:
            assert response.read() == installer.read_bytes()
        tampered = f"/tampered/{candidate.version}/{ASSET_NAME}"
        with opener.open(mirror.base_url + tampered, timeout=5) as response:
            assert hashlib.sha256(response.read()).hexdigest() != candidate.sha256
        with opener.open(mirror.base_url + tampered + ".sha256", timeout=5) as response:
            assert response.read() == candidate.checksum_bytes()
        assert not mirror.unexpected_paths
        with pytest.raises(HTTPError) as failure:
            opener.open(mirror.base_url + "/releases/v0.29.0/" + ASSET_NAME, timeout=5)
        assert failure.value.code == 404
        assert mirror.unexpected_paths == [f"/releases/v0.29.0/{ASSET_NAME}"]
        assert mirror.requested_paths.count("/installer/install.ps1") == 1
    with zipfile.ZipFile(io.BytesIO(candidate.payload)) as archive:
        assert (
            candidate.executable_sha256
            == hashlib.sha256(archive.read(EXECUTABLE_MEMBER)).hexdigest()
        )


@pytest.mark.parametrize("baseline_version", ["v9.8.7", "v9.8.8"])
def test_mirror_rejects_baseline_as_destination(
    candidate_environment: dict[str, str], baseline_version: str
) -> None:
    """The historical binary must be an older upgrade source."""
    candidate = InstallerArchive.from_environment(candidate_environment, "CANDIDATE")
    with (
        pytest.raises(ValueError, match="baseline must be older"),
        serve_installer_candidate(
            candidate, replace(candidate, version=baseline_version), ROOT / "install.ps1"
        ),
    ):
        pytest.fail("The fixture server must not start for a downgrade or reinstall baseline")


@pytest.mark.parametrize("original", [None, ("original user path", 2)])
@pytest.mark.parametrize("interrupted", [False, True])
def test_user_path_restored_even_after_native_timeout(
    monkeypatch: pytest.MonkeyPatch,
    original: tuple[str, int] | None,
    interrupted: bool,
) -> None:
    """The parent owns rollback when killing PowerShell prevents its finally."""
    registry = MagicMock()
    key = registry.OpenKey.return_value.__enter__.return_value
    registry.QueryValueEx.side_effect = [
        FileNotFoundError() if original is None else original,
        ("installer-modified-path", 1),
    ]
    monkeypatch.setitem(sys.modules, "winreg", registry)
    failure = pytest.raises(RuntimeError, match="timed out") if interrupted else nullcontext()
    with failure, preserve_windows_user_path():
        if interrupted:
            raise RuntimeError("Native process tree timed out")
    if original is None:
        registry.DeleteValue.assert_called_once_with(key, "Path")
        registry.SetValueEx.assert_not_called()
    else:
        registry.SetValueEx.assert_called_once_with(key, "Path", 0, original[1], original[0])
        registry.DeleteValue.assert_not_called()


@pytest.mark.skipif(PWSH is None, reason="PowerShell parser is not installed")
def test_powershell_candidate_version_rejects_wrong_binary() -> None:
    """Run the actual suite's comparison function, including substring twins."""
    command = r"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    'scripts/windows/test-install-script.ps1', [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Test-ReportedVersion'
}, $true)
. ([scriptblock]::Create($function.Extent.Text))
if (-not (Test-ReportedVersion 'apm, version 9.8.7 (abcdef123)' 'v9.8.7')) {
    throw 'Exact candidate version was rejected'
}
foreach ($wrong in @('9.8.70', '9.8.7rc1', '9.8.6', '19.8.7', '9x8x7')) {
    if (Test-ReportedVersion "apm, version $wrong" 'v9.8.7') {
        throw "Wrong binary version accepted: $wrong"
    }
}
Write-Output 'candidate version negative twins passed'
"""
    result = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "candidate version negative twins passed"


def test_native_gate_retains_integrity_self_update_and_environment_boundaries() -> None:
    """Local transport must not replace the actual updater or launcher process."""
    script = SUITE.read_text(encoding="utf-8")
    assert "$PinnedVersion" not in script
    assert "[Parameter(Mandatory = $true)][string]$CandidateVersion" in script
    assert '$env:APM_NO_DIRECT_FALLBACK = "1"' in script
    assert "Remove-Item Env:APM_SKIP_CHECKSUM" in script
    assert 'cmd.exe /c "`"$shim`" self-update"' in script
    assert "for ($attempt" not in script
    assert "Test-CandidateChecksumRejection" in script[script.index("# Runner") :]
    assert "Assert-CandidatePayload -Root $prefix.Root" in script
    assert "Test-ReportedVersion $ver2.Output $CandidateVersion" in script
    fresh = script[
        script.index("function Test-EndToEndInstall") : script.index(
            "function Test-NonJunctionCollision"
        )
    ]
    assert "Test-SameVersionReinstall -Prefix $prefix" in fresh
    updater = script[script.index("function Test-SelfUpdateCommand") : script.index("# Runner")]
    assert "Test-CrossVersionUpgrade -Prefix $prefix" in updater
    assert script.count("Invoke-InstallScript -Version $OlderVersion") == 1
    assert "Reinstall replaces the previous release tree, including its canary" in script
    assert '[Environment]::SetEnvironmentVariable("Path", $savedUserPath, "User")' in script
    assert (
        '[Environment]::SetEnvironmentVariable($name, $savedEnvironment[$name], "Process")'
        in script
    )
