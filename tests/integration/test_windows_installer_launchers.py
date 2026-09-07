"""End-to-end regression coverage for Windows installer launchers.

Required inputs: APM_CANDIDATE_{ARCHIVE,VERSION,SHA256} and
APM_BASELINE_{ARCHIVE,VERSION,SHA256}. Versions accept packager metadata
(X.Y.Z) or release tags (vX.Y.Z); the baseline is an older source only.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.isolated_apm_environment import IsolatedApmEnvironment
from tests.utils.windows_installer_candidate import (
    ASSET_NAME,
    InstallerArchive,
    preserve_windows_user_path,
    serve_installer_candidate,
)

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.requires_e2e_mode,
    pytest.mark.requires_windows,
]

ROOT = Path(__file__).resolve().parents[2]


def test_windows_installer_exposes_stable_executable(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Install APM and launch bare ``apm`` through Windows process boundaries."""
    candidate = InstallerArchive.from_environment(os.environ, "CANDIDATE")
    baseline = InstallerArchive.from_environment(os.environ, "BASELINE")
    # Keep the harness prefix short; the PowerShell fixture still adds spaces and "&".
    isolated = IsolatedApmEnvironment.create(
        tmp_path_factory.mktemp("wi") / "i", base_env=os.environ
    )
    environment = isolated.subprocess_env()
    environment["APPDATA"] = str(isolated.home)
    with (
        preserve_windows_user_path(),
        serve_installer_candidate(candidate, baseline, ROOT / "install.ps1") as mirror,
    ):
        environment["APM_TEST_LOOPBACK_PORTS"] = str(urlparse(mirror.base_url).port)
        runner = ApmLifecycleRunner(
            [
                "pwsh",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(ROOT / "scripts" / "windows" / "test-install-script.ps1"),
            ],
            timeout_seconds=600,
        )
        result = runner.run(
            [
                "-CandidateVersion",
                candidate.version,
                "-OlderVersion",
                baseline.version,
                "-CandidateExecutableSha256",
                candidate.executable_sha256,
                "-FixtureBaseUrl",
                mirror.base_url,
                "-TestRoot",
                str(isolated.work_root),
            ],
            cwd=ROOT,
            env=environment,
            scenario_id="windows-candidate-installer",
        )

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.rstrip().endswith("All install.ps1 integration tests passed.")
        assert not mirror.unexpected_paths
        assert mirror.requested_paths.count("/installer/install.ps1") == 1
        for release in (candidate, baseline):
            path = f"/releases/{release.version}/{ASSET_NAME}"
            assert path in mirror.requested_paths
            assert path + ".sha256" in mirror.requested_paths
        assert mirror.requested_paths.count(f"/releases/{baseline.version}/{ASSET_NAME}") == 1
        assert 3 <= mirror.requested_paths.count(f"/releases/{candidate.version}/{ASSET_NAME}") <= 4
        tampered_path = f"/tampered/{candidate.version}/{ASSET_NAME}"
        assert tampered_path in mirror.requested_paths
        assert tampered_path + ".sha256" in mirror.requested_paths
