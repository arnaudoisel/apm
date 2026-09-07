"""Scoped pytest observer for freshly executed lifecycle contract witnesses."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from scripts.lifecycle_contracts import EvidenceError, command_inventory, command_path
from tests.utils.apm_lifecycle_runner import ApmLifecycleRunner
from tests.utils.artifact_snapshot import ArtifactSnapshotSet
from tests.utils.isolated_apm_environment import DURABLE_ENVIRONMENT_ROOTS


def fingerprint(path: Path) -> str:
    """Hash an executable or source artifact without trusting its filename."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot(cwd: Path, env: dict[str, str]) -> str:
    """Use the existing open-world oracle for project and isolated user roots."""
    candidates = [cwd, *(Path(env[key]) for key in DURABLE_ENVIRONMENT_ROOTS if env.get(key))]
    roots: list[Path] = []
    for candidate in sorted({path.resolve() for path in candidates}, key=lambda p: len(p.parts)):
        if not any(candidate.is_relative_to(root) for root in roots):
            roots.append(candidate)
    captured = ArtifactSnapshotSet.capture({str(path): path for path in roots})
    payload = [
        (name, value.root_existed, [asdict(entry) for entry in value.entries])
        for name, value in captured.snapshots
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


class LifecycleEvidencePlugin:
    """Observe exact nodes and runner invocations, never authored outcome strings."""

    def __init__(self, nodeids: list[str], executable: Path) -> None:
        self.nodeids = set(nodeids)
        self.executable = executable.resolve()
        self.executable_hash = fingerprint(self.executable)
        self.python_hash = fingerprint(Path(sys.executable).resolve())
        self.inventory = command_inventory()
        self.records: dict[str, dict[str, Any]] = {}
        self.active: str | None = None
        self.phase: str | None = None
        self.model_run: int | None = None
        self.patch = pytest.MonkeyPatch()

    def pytest_sessionstart(self, session: pytest.Session) -> None:
        """Install observers before test-module imports bind runner/model functions."""
        from hypothesis import stateful

        original = ApmLifecycleRunner._run_with_timeout
        model = stateful.run_state_machine_as_test

        def observe(runner: ApmLifecycleRunner, args: Any, **kwargs: Any) -> Any:
            if self.active not in self.nodeids or self.phase != "call":
                return original(runner, args, **kwargs)
            command = runner._command
            source_command = (sys.executable, "-m", "apm_cli.cli")
            if command != source_command and command != (str(self.executable),):
                raise EvidenceError(f"Unverified source executable: {command}")
            if (
                fingerprint(self.executable) != self.executable_hash
                or fingerprint(Path(sys.executable).resolve()) != self.python_hash
            ):
                raise EvidenceError("Executable changed during evidence execution")
            cwd, env = kwargs["cwd"], kwargs["env"]
            before = snapshot(cwd, env)
            result = original(runner, args, **kwargs)
            after = snapshot(cwd, env)
            self.records[self.active]["events"].append(
                {
                    "command": command_path(list(args), self.inventory),
                    "args": list(args),
                    "returncode": result.returncode,
                    "cwd": str(cwd.resolve()),
                    "before": before,
                    "after": after,
                    "scenario_id": kwargs["scenario_id"],
                    "entrypoint": list(command),
                    "model": self.model_run,
                }
            )
            return result

        def observe_model(*args: Any, **kwargs: Any) -> Any:
            if self.active not in self.nodeids or self.phase != "call":
                return model(*args, **kwargs)
            previous = self.model_run
            self.model_run = self.records[self.active]["models"] + 1
            try:
                result = model(*args, **kwargs)
                self.records[self.active]["models"] += 1
                return result
            finally:
                self.model_run = previous

        self.patch.setattr(ApmLifecycleRunner, "_run_with_timeout", observe)
        self.patch.setattr(stateful, "run_state_machine_as_test", observe_model)

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        """Final selection, not an AST function lookup or pre-deselection inventory."""
        for item in session.items:
            if item.nodeid not in self.nodeids:
                continue
            callspec = getattr(item, "callspec", None)
            self.records[item.nodeid] = {
                "collected": True,
                "dimensions": dict(callspec.params) if callspec else {},
                "phases": {},
                "events": [],
                "models": 0,
            }

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item, nextitem: pytest.Item | None) -> Any:
        """Keep fixture execution associated with its exact collected node."""
        self.active = item.nodeid
        yield
        self.active, self.phase = None, None

    def pytest_runtest_setup(self, item: pytest.Item) -> None:
        self.phase = "setup"

    def pytest_runtest_call(self, item: pytest.Item) -> None:
        self.phase = "call"

    def pytest_runtest_teardown(self, item: pytest.Item, nextitem: pytest.Item | None) -> None:
        self.phase = "teardown"

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.nodeid in self.records:
            outcome = "xfail" if hasattr(report, "wasxfail") else report.outcome
            self.records[report.nodeid]["phases"][report.when] = outcome

    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> None:
        """Restore instrumentation even when collection or test setup fails."""
        self.patch.undo()
