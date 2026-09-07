"""Hermetic regressions for the required-check aggregator's shell boundary."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.workflow_contracts import load_workflow, workflow_job, workflow_step

pytestmark = [
    pytest.mark.component,
    pytest.mark.skipif(os.name == "nt", reason="The merge gate executes on a POSIX runner"),
]

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/ci/merge_gate_wait.sh"
SHA = "a" * 40
GateRunner = Callable[..., subprocess.CompletedProcess[str]]

FAKE_TOOLS = """\
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["GATE_TEST_ROOT"])
tool = Path(sys.argv[0]).name
if tool == "date":
    clock = root / "clock"
    tick = int(clock.read_text()) if clock.exists() else 0
    clock.write_text(str(tick + 1))
    print(tick if tick <= int(os.environ["GATE_TEST_POLLS"]) else 61)
elif tool == "gh":
    with (root / "requests").open("a") as stream:
        stream.write(json.dumps(sys.argv[1:]) + "\\n")
    state_path = root / "calls"
    index = int(state_path.read_text()) if state_path.exists() else 0
    state_path.write_text(str(index + 1))
    pages = []
    for rounds in json.loads((root / "responses").read_text()).values():
        response = rounds[min(index, len(rounds) - 1)]
        if response == "api-error":
            sys.exit(1)
        if isinstance(response, str):
            print(response)
            sys.exit(0)
        pages.extend(response)
    print(json.dumps(pages))
"""


def _check(
    name: str = "Lint",
    *,
    conclusion: str | None = "success",
    status: str = "completed",
    app: str = "github-actions",
    sha: str = SHA,
) -> dict[str, object]:
    """Build one Checks API result with explicit source identity."""
    return {
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion,
        "app": {"slug": app},
        "html_url": "https://github.com/microsoft/apm/actions/runs/123/job/456",
    }


def _pages(*checks: dict[str, object]) -> list[dict[str, object]]:
    """Match gh api --paginate --slurp output without making network calls."""
    return [{"check_runs": list(checks)}]


@pytest.fixture
def run_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GateRunner:
    """Run the real Bash/JQ logic with a deterministic clock and fake GitHub."""
    assert shutil.which("bash"), "bash is required to exercise the deployed gate"
    assert shutil.which("jq"), "jq is required to exercise the deployed gate"
    for command in ("gh", "date", "sleep"):
        tool = tmp_path / command
        tool.write_text(f"#!{sys.executable}\n{FAKE_TOOLS}", encoding="ascii")
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GATE_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("GH_TOKEN", "fake-token")
    monkeypatch.setenv("REPO", "microsoft/apm")
    monkeypatch.setenv("SHA", SHA)
    monkeypatch.setenv("TIMEOUT_MIN", "1")
    monkeypatch.setenv("POLL_SEC", "0")

    def run(
        responses: dict[str, list[object]],
        *,
        polls: int = 1,
        event: str = "pull_request",
        script: Path = SCRIPT,
    ) -> subprocess.CompletedProcess[str]:
        (tmp_path / "responses").write_text(json.dumps(responses), encoding="ascii")
        monkeypatch.setenv("EXPECTED_CHECKS", ",".join(responses))
        monkeypatch.setenv("GATE_TEST_POLLS", str(polls))
        monkeypatch.setenv("EVENT_NAME", event)
        return subprocess.run(
            ["bash", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

    return run


def test_all_required_successes_pass_in_the_final_poll(
    run_gate: GateRunner, tmp_path: Path
) -> None:
    """Success must not require another sleep/poll beyond the deadline."""
    names = ("Lint", "Test Architecture Ratchets", "Build & Test Shard 1 (Linux)")
    result = run_gate({name: [_pages(_check(name))] for name in names})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "all 3 check(s) completed successfully" in result.stdout
    calls = [json.loads(line) for line in (tmp_path / "requests").read_text().splitlines()]
    assert len(calls) == 1
    arguments = calls[0]
    assert "--paginate" in arguments
    assert "--slurp" in arguments
    parsed = urlparse(arguments[-1])
    assert parsed.path == f"repos/microsoft/apm/commits/{SHA}/check-runs"
    assert parse_qs(parsed.query) == {"filter": ["latest"], "per_page": ["100"]}


@pytest.mark.parametrize(
    "conclusion",
    [
        "skipped",
        "neutral",
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
        None,
        "unknown",
    ],
)
def test_required_check_without_success_fails_closed(
    run_gate: GateRunner, conclusion: str | None
) -> None:
    result = run_gate({"Lint": [_pages(_check(conclusion=conclusion))]})

    assert result.returncode == 1
    assert "Required check failed" in result.stdout
    assert "completed successfully" not in result.stdout


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
@pytest.mark.parametrize("response", [_pages(), "api-error", "invalid-json"])
def test_missing_or_unreadable_required_check_blocks_with_recovery(
    run_gate: GateRunner, response: object, event: str
) -> None:
    result = run_gate({"Lint": [response]}, event=event)

    assert result.returncode == 2
    assert "Required check never started" in result.stderr
    assert "Lint" in result.stderr
    if event == "merge_group":
        assert "Remove the PR from the merge queue and re-add it" in result.stderr
    else:
        assert "close and reopen the PR" in result.stderr


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_unfinished_required_check_times_out(run_gate: GateRunner, status: str) -> None:
    result = run_gate({"Lint": [_pages(_check(status=status, conclusion=None))]})

    assert result.returncode == 3
    assert "Required check timeout" in result.stderr
    urls = [urlparse(token) for token in result.stderr.split() if "://" in token]
    assert [(url.hostname, url.path) for url in urls] == [
        ("github.com", "/microsoft/apm/actions/runs/123/job/456")
    ]


@pytest.mark.parametrize(
    ("first_response", "timeout_code"),
    [
        (_pages(), 2),
        (_pages(_check("Test Architecture Ratchets", status="queued", conclusion=None)), 3),
    ],
)
@pytest.mark.parametrize("polls", [1, 2])
def test_partial_success_waits_for_every_required_check(
    run_gate: GateRunner, first_response: object, timeout_code: int, polls: int
) -> None:
    """A late check may recover, but cannot be replaced by another check's pass."""
    result = run_gate(
        {
            "Lint": [_pages(_check())],
            "Test Architecture Ratchets": [
                first_response,
                _pages(_check("Test Architecture Ratchets")),
            ],
        },
        polls=polls,
    )

    assert result.returncode == (timeout_code if polls == 1 else 0)
    assert ("completed successfully" in result.stdout) is (polls == 2)


@pytest.mark.parametrize(
    "override",
    [{"app": "another-app"}, {"name": "Other Lint"}, {"sha": "b" * 40}],
)
def test_wrong_check_identity_cannot_satisfy_requirement(
    run_gate: GateRunner, override: dict[str, str]
) -> None:
    result = run_gate({"Lint": [_pages(_check(**override))]})

    assert result.returncode == 2
    assert "Required check never started" in result.stderr


def test_external_app_collision_does_not_hide_actions_failure(run_gate: GateRunner) -> None:
    result = run_gate({"Lint": [_pages(_check(conclusion="failure"), _check(app="another-app"))]})

    assert result.returncode == 1
    assert "FAILED (failure)" in result.stdout


@pytest.mark.parametrize("other_conclusion", ["success", "failure"])
def test_duplicate_actions_names_on_different_pages_fail_closed(
    run_gate: GateRunner, other_conclusion: str
) -> None:
    result = run_gate({"Lint": [[*_pages(_check()), *_pages(_check(conclusion=other_conclusion))]]})

    assert result.returncode == 1
    assert "Ambiguous required check" in result.stdout


def test_rerun_cannot_reuse_an_earlier_success(run_gate: GateRunner) -> None:
    result = run_gate(
        {
            "Lint": [_pages(_check()), _pages(_check(conclusion="failure"))],
            "Test Architecture Ratchets": [
                _pages(_check("Test Architecture Ratchets", status="in_progress", conclusion=None)),
                _pages(_check("Test Architecture Ratchets")),
            ],
        },
        polls=2,
    )

    assert result.returncode == 1
    assert "FAILED (failure)" in result.stdout


@pytest.mark.parametrize("conclusion", ["skipped", "neutral"])
def test_accepting_non_success_mutation_is_detected(
    run_gate: GateRunner, tmp_path: Path, conclusion: str
) -> None:
    """A policy-relaxing mutation produces the forbidden green verdict."""
    source = SCRIPT.read_text(encoding="ascii")
    assert "\n      success)\n" in source
    mutant = tmp_path / "mutant.sh"
    mutant.write_text(
        source.replace("\n      success)\n", f"\n      success|{conclusion})\n"),
        encoding="ascii",
    )
    result = run_gate({"Lint": [_pages(_check(conclusion=conclusion))]}, script=mutant)

    with pytest.raises(AssertionError):
        assert result.returncode == 1


def test_gate_retains_read_only_token_and_trusted_base_checkout() -> None:
    """PRs must not execute their own gate script or receive production secrets."""
    workflow = load_workflow(ROOT / ".github/workflows/merge-gate.yml")
    assert set(workflow["on"]) == {"pull_request", "merge_group"}
    assert workflow["permissions"] == {
        "contents": "read",
        "checks": "read",
        "pull-requests": "read",
    }
    gate = workflow_job(workflow, "gate")
    checkout = workflow_step(gate, "Checkout trusted gate implementation")
    assert checkout["with"]["ref"] == "${{ steps.sha.outputs.trusted_sha }}"
    assert checkout["with"]["persist-credentials"] is False
    resolver = workflow_step(gate, "Resolve target and trusted SHAs")["run"]
    for expression in (
        "github.event.pull_request.base.sha",
        "github.event.merge_group.base_sha",
    ):
        assert expression in resolver
    wait = workflow_step(gate, "Wait for all required checks")
    assert wait["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert wait["env"]["SHA"] == "${{ steps.sha.outputs.target_sha }}"
    assert "secrets." not in json.dumps(workflow)
