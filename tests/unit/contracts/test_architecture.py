"""Regression traps for contract ownership and legacy execution bypasses."""

from pathlib import Path

import pytest

from scripts.architecture_linter.checks.contract_leaf_runtime import check_contract_owners
from scripts.architecture_linter.facts import FactsProvider

pytestmark = pytest.mark.component


@pytest.mark.parametrize(
    ("filename", "source"),
    [
        ("frontend.py", "from ..core.script_runner import ScriptRunner\n"),
        ("engine.py", "subprocess.Popen(command)\n"),
        ("workspace.py", "digest = compute_file_hash(path)\n"),
        ("engine.py", "def reduce_outcome():\n    return 0\n"),
        ("engine.py", "RuntimeFactory.get_best_available_runtime()\n"),
    ],
)
def test_contract_owner_bypasses_are_rejected(tmp_path: Path, filename: str, source: str) -> None:
    path = f"src/apm_cli/contracts/{filename}"
    provider = FactsProvider(tmp_path, (path,), None, source_overrides={path: source})
    findings = check_contract_owners(provider)
    assert len(findings) == 1
    assert findings[0].path == path
    assert findings[0].line >= 1


def test_contract_owner_routes_are_accepted(tmp_path: Path) -> None:
    sources = {
        "src/apm_cli/contracts/process.py": "subprocess.Popen(request.argv)\n",
        "src/apm_cli/contracts/records.py": "def reduce_outcome():\n    return result\n",
        "src/apm_cli/contracts/engine.py": "result = records.reduce_outcome()\n",
    }
    provider = FactsProvider(tmp_path, tuple(sources), None, source_overrides=sources)
    assert check_contract_owners(provider) == ()
