---
title: Run a contract
description: Plan and run a local file-producing contract in a disposable workspace.
sidebar:
  order: 4
---

Use a contract when you need one generated file assessed by named checks.
Existing [scripts](../run-scripts/) still use `apm run` without `--on`.

## Prerequisites

- macOS or Linux, an installed APM build with Contracts v0.1, and native Copilot
  ready on `PATH` with access to your selected model.
- Git and Python 3 on `PATH`; this fixture's checker uses only Python's standard
  library.
- An independent, disposable, secret-free local workspace with no Git remote or
  configured policy requirement. Governed or unresolved policy is unsupported.

Native Copilot `1.0.83-5` was exercised with these fixtures; `plan` does not
probe versions.

**Native execution is not isolated.** Read the
[host-access, consent, and policy limits](../../reference/cli/run/#native-execution-boundary).
Do not copy a governed repository or remove its remotes to obtain eligibility.

## Copy the supplied fixture

The APM source checkout supplies `examples/contracts/first-contract/`:
`apm.yml` (no dependencies), `notes.md`, `handoff.contract.md`, and
`checks/check_handoff.py`. From that checkout, copy only the authored fixtures
to a fresh workspace, not the repository:

```bash
workspace="$(mktemp -d)"
cp -R examples/contracts/. "$workspace/"
cd "$workspace/first-contract"
```

Use your normal installed CLI; no install step is needed for this dependency-free
fixture. Run from its `apm.yml` directory:

```bash
apm plan ./handoff.contract.md --on copilot --model gpt-6-astra
apm run ./handoff.contract.md --on copilot --model gpt-6-astra --allow-advisory
```

The example explicitly requests `gpt-6-astra`, not an APM default. Requested and
observed models are recorded separately. A
[successful plan](../../reference/cli/plan/#read-only-planning) is not a completed
assessment.

## Inspect the result

The contract asks Copilot to write a JSON array with `source_id`, `summary`,
and `caution` strings, using the exact IDs in `notes.md`, without executing the
commands described there. The helper checks JSON shape, exact ID coverage,
duplicates, and nonempty strings. It does **not** guarantee factual accuracy
or prose quality.

Follow the reported artifact and record paths under `.apm/runs/<run-id>/`.
The original project output is not overwritten. Use the
[outcome table](../../reference/cli/run/#results-and-retained-files) to distinguish
a failed criterion, missing/incomplete evidence, and an operational stop.
Inspect private logs locally; they are not guaranteed safe to share.

## Try installed context

The sibling `reuse-contract` fixture declares the supplied local `handoff-style`
skill and reuses the first fixture's maintained notes and checker. Prepare those
resources in the copied workspace, then enter the fixture and install explicitly:

```bash
cd "$workspace"
mkdir -p reuse-contract/checks
cp first-contract/notes.md reuse-contract/notes.md
cp first-contract/checks/check_handoff.py reuse-contract/checks/check_handoff.py
cd reuse-contract
apm install --only apm --target copilot
apm plan ./handoff.contract.md --on copilot --model gpt-6-astra
apm run ./handoff.contract.md --on copilot --model gpt-6-astra --allow-advisory
```

This supplies selected context, not trusted pinning or automatic native skill
activation. See [import eligibility](../../reference/cli/plan/#imported-skill-context)
and the [source-format reference](../../reference/cli/plan/#contract-source)
before adapting a fixture.
