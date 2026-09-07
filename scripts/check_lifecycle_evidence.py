#!/usr/bin/env python3
"""Execute lifecycle obligations for an exact candidate; never import receipts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Direct script execution must resolve first-party helpers from this checkout.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.lifecycle_contracts import (  # noqa: E402
    LEDGER,
    EvidenceError,
    command_inventory,
    git,
    load_ledger,
    select_contracts,
    validate_execution,
)

LIMITATIONS = [
    "Applicability, assertions and model semantics require code-owner review.",
    "PR code and its verifier are not tamper-proof; protected review is the trust boundary.",
    "Bounded source-Python trajectories do not establish packaged or exhaustive parity.",
    "A pr-lane pending report is not shipping evidence; execute lane full independently.",
]


def source_profile(root: Path) -> tuple[Path, dict[str, str]]:
    """Identify the installed editable source and its exact interpreter/launcher."""
    import apm_cli
    from tests.utils.lifecycle_evidence import fingerprint

    if Path(apm_cli.__file__).resolve().parent != root / "src/apm_cli":
        raise EvidenceError("Installed source does not resolve to the candidate checkout")
    executable = Path(sys.executable).parent / ("apm.exe" if os.name == "nt" else "apm")
    if os.name == "nt" or not executable.is_file():
        raise EvidenceError("source-python profile requires the Unix installed apm script")
    script = executable.read_text(encoding="utf-8")
    if "apm_cli.cli" not in script or not script.startswith(f"#!{sys.executable}\n"):
        raise EvidenceError("Installed apm entrypoint is not this Python environment")
    return executable, {
        "kind": "source-python",
        "executable": str(executable),
        "executable_sha256": fingerprint(executable),
        "python": str(Path(sys.executable).resolve()),
        "python_sha256": fingerprint(Path(sys.executable).resolve()),
    }


def candidate(root: Path, base: str, head: str) -> dict[str, str]:
    """Require explicit existing revisions and a pristine tested source tree."""
    resolved_base = git(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}")
    resolved_head = git(root, "rev-parse", "--verify", "--end-of-options", f"{head}^{{commit}}")
    if resolved_head != git(root, "rev-parse", "HEAD"):
        raise EvidenceError("Requested head is not the checked-out candidate")
    if resolved_base == resolved_head:
        raise EvidenceError("Base and head must describe a candidate change")
    git(root, "merge-base", "--is-ancestor", resolved_base, resolved_head)
    if git(root, "status", "--porcelain", "--untracked-files=normal"):
        raise EvidenceError("Candidate must be clean, including untracked files")
    return {
        "base": resolved_base,
        "head": resolved_head,
        "tested_tree": git(root, "rev-parse", "HEAD^{tree}"),
    }


def execute(args: argparse.Namespace) -> dict[str, Any]:
    """Generate native evidence from this process's own fresh pytest execution."""
    identity = candidate(ROOT, args.base, args.head)
    report: dict[str, Any] = {
        "version": 1,
        **identity,
        "lane": args.lane,
        "status": "blocked",
        "profile": {"kind": "source-python"},
        "contracts": [],
        "witnesses": {},
        "limitations": LIMITATIONS,
    }
    try:
        inventory = command_inventory()
        base_text = git(ROOT, "show", f"{identity['base']}:{LEDGER}")
        base = load_ledger(base_text)
        head = load_ledger((ROOT / LEDGER).read_text(encoding="utf-8"))
        changed = set(
            git(
                ROOT,
                "diff",
                "--name-only",
                "--no-renames",
                "-z",
                identity["base"],
                identity["head"],
            ).split("\0")
        ) - {""}
        contracts = select_contracts(base, head, changed, inventory)
        report["contracts"] = [contract["id"] for contract in contracts]
        report["command_inventory"] = sorted(inventory)
        report["changed_paths"] = sorted(changed)
        if not contracts:
            if candidate(ROOT, args.base, args.head) != identity:
                raise EvidenceError("Candidate identity changed during assessment")
            report["status"] = "not_applicable"
            return report
        witnesses = [
            witness
            for contract in contracts
            for witness in contract["witnesses"]
            if args.lane == "full" or witness["kind"] == "deterministic"
        ]
        if not witnesses:
            raise EvidenceError("Empty lifecycle witness selection")

        import pytest

        from tests.utils.lifecycle_evidence import LifecycleEvidencePlugin

        executable, report["profile"] = source_profile(ROOT)
        nodeids = sorted({witness["nodeid"] for witness in witnesses})
        plugin = LifecycleEvidencePlugin(nodeids, executable)
        os.environ["APM_BINARY_PATH"] = str(executable)
        os.environ["APM_E2E_TESTS"] = "1"
        # No user-provided pytest options, deselection filters or receipt inputs.
        os.environ.pop("PYTEST_ADDOPTS", None)
        exitcode = pytest.main(
            ["-p", "no:cacheprovider", "-o", "addopts=", "--strict-markers", "-q", *nodeids],
            plugins=[plugin],
        )
        report["witnesses"] = plugin.records
        if exitcode != 0:
            raise EvidenceError(f"Lifecycle pytest execution failed with exit code {exitcode}")
        for witness in witnesses:
            validate_execution(witness, plugin.records.get(witness["nodeid"], {}))
        if candidate(ROOT, args.base, args.head) != identity:
            raise EvidenceError("Candidate identity changed during execution")
        if source_profile(ROOT)[1] != report["profile"]:
            raise EvidenceError("Source executable identity changed during execution")
        report["status"] = "passed" if args.lane == "full" else "pending"
    except (EvidenceError, KeyError, TypeError, ValueError) as exc:
        report["error"] = str(exc)
    return report


def main() -> int:
    """CLI entrypoint; nonzero means blocked, pending is explicitly lane-scoped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--lane", choices=("pr", "full"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.resolve().is_relative_to(ROOT):
        parser.error("--report must be outside the candidate checkout")
    try:
        report = execute(args)
    except (EvidenceError, subprocess.CalledProcessError) as exc:
        report = {
            "version": 1,
            "base": args.base,
            "head": args.head,
            "tested_tree": None,
            "lane": args.lane,
            "status": "blocked",
            "error": str(exc),
            "witnesses": {},
            "limitations": LIMITATIONS,
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="ascii")
    print(f"Lifecycle evidence: {report['status']} ({args.report})")
    return 1 if report["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
