"""Verified, loopback-only release fixtures for the native Windows installer."""

from __future__ import annotations

import hashlib
import io
import re
import threading
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from packaging.version import Version

ASSET_NAME = "apm-windows-x86_64.zip"
EXECUTABLE_MEMBER = "apm-windows-x86_64/apm.exe"


@contextmanager
def preserve_windows_user_path() -> Iterator[None]:
    """Restore persistent PATH even when the native process tree times out."""
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_SET_VALUE
    ) as key:
        try:
            original = winreg.QueryValueEx(key, "Path")
        except FileNotFoundError:
            original = None
        try:
            yield
        finally:
            try:
                current = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                current = None
            if current != original:
                if original is None:
                    winreg.DeleteValue(key, "Path")
                else:
                    value, kind = original
                    winreg.SetValueEx(key, "Path", 0, kind, value)


@dataclass(frozen=True)
class InstallerArchive:
    """Immutable bytes authenticated against the caller's explicit digest."""

    version: str
    payload: bytes
    sha256: str
    executable_sha256: str

    @classmethod
    def from_environment(cls, environment: Mapping[str, str], role: str) -> InstallerArchive:
        """Require an explicit candidate/baseline; never resolve a public release."""
        prefix = f"APM_{role}_"
        values = {}
        for field in ("ARCHIVE", "VERSION", "SHA256"):
            name = prefix + field
            value = environment.get(name, "")
            if not value:
                raise ValueError(f"{name} is required; no published-release fallback is allowed")
            values[field] = value
        version = values["VERSION"]
        if re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+)?", version) is None:
            raise ValueError(f"{prefix}VERSION must be an exact vX.Y.Z or X.Y.Z release version")
        version = "v" + version.removeprefix("v")
        digest = values["SHA256"]
        if re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise ValueError(f"{prefix}SHA256 must be an explicit SHA256 digest")
        payload = Path(values["ARCHIVE"]).read_bytes()
        digest = digest.lower()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError(f"{role} archive SHA256 mismatch")
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            if archive.namelist().count(EXECUTABLE_MEMBER) != 1:
                raise ValueError(f"{role} archive must contain exactly one {EXECUTABLE_MEMBER}")
            with archive.open(EXECUTABLE_MEMBER) as executable:
                executable_digest = hashlib.file_digest(executable, "sha256").hexdigest()
        return cls(version, payload, digest, executable_digest)

    def checksum_bytes(self) -> bytes:
        """Return the production installer's SHA256 sidecar format."""
        return f"{self.sha256}  {ASSET_NAME}\n".encode("ascii")


@dataclass
class InstallerMirror:
    """One bounded server and its observed production HTTP requests."""

    base_url: str
    requested_paths: list[str]
    unexpected_paths: list[str]


@contextmanager
def serve_installer_candidate(
    candidate: InstallerArchive,
    baseline: InstallerArchive,
    installer: Path,
) -> Iterator[InstallerMirror]:
    """Serve exact verified archives and the unmodified checkout installer."""
    if Version(baseline.version) >= Version(candidate.version):
        raise ValueError("The baseline must be older than the candidate destination")
    routes = {"/installer/install.ps1": installer.read_bytes()}
    for release in (candidate, baseline):
        archive_path = f"/releases/{release.version}/{ASSET_NAME}"
        routes[archive_path] = release.payload
        routes[archive_path + ".sha256"] = release.checksum_bytes()
    tampered_path = f"/tampered/{candidate.version}/{ASSET_NAME}"
    routes[tampered_path] = candidate.payload + b"\nchecksum-negative-twin\n"
    routes[tampered_path + ".sha256"] = candidate.checksum_bytes()
    requested: list[str] = []
    unexpected: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested.append(self.path)
            body = routes.get(self.path)
            if body is None:
                unexpected.append(self.path)
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            """Keep archive requests out of pytest's normal output."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield InstallerMirror(f"http://127.0.0.1:{server.server_port}", requested, unexpected)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError("Windows installer fixture server did not stop")
